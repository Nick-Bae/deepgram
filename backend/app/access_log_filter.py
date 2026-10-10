"""Defense-in-depth: scrub credential query params from in-container log lines.

F2 follow-up v2. The permanent fix is the subprotocol-based auth path in
`app.auth.ws_auth` which keeps credentials off the URL entirely. This
filter protects the migration window (clients still sending
`?idToken=...` or `?hostToken=...`) from printing credential bytes into
in-container access logs. It does NOT reach Cloud Run's PLATFORM
request log — that log is managed by GCP and is only kept
credential-free by not putting credentials in the URL (which this fix
does).

Covered parameters:
    idToken  / id_token    — Firebase ID token
    hostToken / host_token / token — host-upgrade token (authorization credential)

Installed from `app.main` module import.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

# Match each credential-bearing query param + value independently so a
# URL carrying both is scrubbed in one pass.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"idToken=[^&\s\"']+", re.IGNORECASE),   "idToken=<REDACTED>"),
    (re.compile(r"id_token=[^&\s\"']+", re.IGNORECASE),  "id_token=<REDACTED>"),
    (re.compile(r"hostToken=[^&\s\"']+", re.IGNORECASE), "hostToken=<REDACTED>"),
    (re.compile(r"host_token=[^&\s\"']+", re.IGNORECASE), "host_token=<REDACTED>"),
    # `token=` is an alias accepted by main.py's query-param reader for the
    # host-upgrade token. Match defensively; benign callers shouldn't be
    # affected because non-WS routes don't use this parameter name.
    (re.compile(r"(?<![a-zA-Z_])token=[^&\s\"']+", re.IGNORECASE), "token=<REDACTED>"),
]


def _scrub(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return value
    if not any(k in value.lower() for k in ("idtoken=", "id_token=", "hosttoken=", "host_token=", "token=")):
        return value
    out = value
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


class IdTokenScrubber(logging.Filter):
    """Replace `idToken=<raw>` with `idToken=<REDACTED>` in msg and all string args."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        try:
            if isinstance(record.msg, str):
                scrubbed = _scrub(record.msg)
                if scrubbed is not None and scrubbed != record.msg:
                    record.msg = scrubbed
            if record.args:
                if isinstance(record.args, tuple):
                    record.args = tuple(
                        _scrub(a) if isinstance(a, str) else a for a in record.args
                    )
                elif isinstance(record.args, dict):
                    record.args = {
                        k: (_scrub(v) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
        except Exception:
            # Logging filters MUST NOT raise; swallow and let the line through
            # as-is (defense-in-depth, not the primary mitigation).
            pass
        return True


_INSTALLED = False
_SCRUBBER: Optional["IdTokenScrubber"] = None
_ORIGINAL_LOGGER_ADD_HANDLER = logging.Logger.addHandler  # for monkey-patch


def _install_on_handler(handler: logging.Handler) -> None:
    """Attach the global scrubber to a handler, idempotently."""
    if _SCRUBBER is None:
        return
    # Avoid duplicate attachment.
    if any(isinstance(f, IdTokenScrubber) for f in getattr(handler, "filters", [])):
        return
    handler.addFilter(_SCRUBBER)


def _install_on_all_live_handlers() -> None:
    """Attach the scrubber to every handler currently reachable in the logger tree.

    v4 fix: a `logging.Filter` on a LOGGER is only consulted for records
    emitted THROUGH that logger directly — records propagating UP from
    child loggers hit the ancestor's HANDLERS without the ancestor's
    LOGGER filters being invoked. Installing on handlers covers the
    child-logger case.

    v5 fix: also iterate `logging.Logger.manager.loggerDict` and attach
    to EVERY handler on EVERY existing logger (not just root + the
    well-known uvicorn/fastapi loggers). This closes the gap for
    pre-install handlers attached to arbitrary loggers (e.g., a
    custom module that configured its own handler before main.py
    imports). Also covers PlaceHolder entries by skipping them.
    """
    # Root handlers (handles the common case where propagated child-logger
    # records reach the root handler chain).
    for h in logging.getLogger().handlers:
        _install_on_handler(h)
    # Every reachable named logger (including app.*, uvicorn.*, etc).
    seen: set[int] = set()
    try:
        for _name, logger_obj in list(logging.Logger.manager.loggerDict.items()):
            if not isinstance(logger_obj, logging.Logger):
                # PlaceHolder entries — nothing to attach to.
                continue
            for h in logger_obj.handlers:
                if id(h) in seen:
                    continue
                seen.add(id(h))
                _install_on_handler(h)
    except Exception:
        # Logging teardown ordering oddities during shutdown — swallow;
        # the root-handler path above already handles the common case.
        pass


def install_id_token_scrubber() -> None:
    """Idempotent install.

    Attaches the scrubber to:
      1. every handler currently on the root logger and the named loggers
         uvicorn.access / uvicorn / uvicorn.error / fastapi;
      2. any handler added LATER via `logging.Logger.addHandler` (via a
         module-level monkey-patch that wraps the method);
    AND, as a belt-and-suspenders backup, to the named loggers themselves
    (same as v2 — covers records emitted directly through those loggers).

    The handler-level attachment is the one that covers records
    propagating from descendant loggers; see commit note.
    """
    global _INSTALLED, _SCRUBBER
    if _INSTALLED:
        return
    _SCRUBBER = IdTokenScrubber()

    # Belt: filter on named loggers (covers logger.info() emitted on those loggers directly).
    for name in ("uvicorn.access", "uvicorn", "uvicorn.error", "fastapi"):
        logging.getLogger(name).addFilter(_SCRUBBER)
    logging.getLogger().addFilter(_SCRUBBER)

    # Suspenders: filter on every reachable HANDLER (covers records that
    # propagate from DESCENDANT loggers up to an ancestor's handler).
    _install_on_all_live_handlers()

    # Monkey-patch addHandler so handlers added AFTER install (e.g., when
    # uvicorn configures logging post-import) also get the scrubber.
    def _patched_add_handler(self: logging.Logger, handler: logging.Handler) -> None:
        _ORIGINAL_LOGGER_ADD_HANDLER(self, handler)
        try:
            _install_on_handler(handler)
        except Exception:
            pass

    logging.Logger.addHandler = _patched_add_handler  # type: ignore[assignment]

    _INSTALLED = True


def _uninstall_id_token_scrubber_for_tests() -> None:
    """Teardown helper for tests. Removes filters and restores addHandler."""
    global _INSTALLED, _SCRUBBER
    if _SCRUBBER is None:
        return
    for name in ("uvicorn.access", "uvicorn", "uvicorn.error", "fastapi", None):
        lg = logging.getLogger(name) if name else logging.getLogger()
        try:
            lg.removeFilter(_SCRUBBER)
        except Exception:
            pass
    # Remove from all live handlers.
    for lg_name in (None, "uvicorn.access", "uvicorn", "uvicorn.error", "fastapi"):
        lg = logging.getLogger(lg_name) if lg_name else logging.getLogger()
        for h in list(lg.handlers):
            try:
                h.removeFilter(_SCRUBBER)
            except Exception:
                pass
    logging.Logger.addHandler = _ORIGINAL_LOGGER_ADD_HANDLER  # type: ignore[assignment]
    _SCRUBBER = None
    _INSTALLED = False
