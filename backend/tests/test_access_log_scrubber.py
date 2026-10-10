"""F2 follow-up: uvicorn.access log scrubber for `idToken=...`."""
from __future__ import annotations

import logging

from app.access_log_filter import IdTokenScrubber, _scrub, install_id_token_scrubber


def _mk_record(msg: str, args: tuple = ()) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_scrub_strips_idtoken_from_bare_string():
    url = "WebSocket /ws/translate?orgId=x&roomId=y&idToken=eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhYmMifQ.sig"
    out = _scrub(url)
    assert out is not None
    assert "idToken=<REDACTED>" in out
    assert "eyJ" not in out


def test_filter_on_record_msg_only():
    f = IdTokenScrubber()
    r = _mk_record(
        'WebSocket /ws/translate?orgId=x&idToken=eyJhbGciOiJSUzI1NiJ9.payload.sig HTTP/1.1'
    )
    assert f.filter(r) is True
    assert "idToken=<REDACTED>" in r.msg
    assert "eyJ" not in r.msg


def test_filter_on_uvicorn_access_args_tuple():
    """Uvicorn access emits via `logger.info('%s - "%s %s HTTP/%s" %d %s', client, method, url, ...)`.

    The URL arrives as a string in record.args, not in record.msg.
    """
    f = IdTokenScrubber()
    r = _mk_record(
        '%s - "%s %s HTTP/%s" %d %s',
        args=(
            "169.254.169.126:12345",
            "GET",
            "/ws/translate?idToken=eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhYmMifQ.sig",
            "1.1",
            200,
            "OK",
        ),
    )
    f.filter(r)
    rendered = r.getMessage()
    assert "idToken=<REDACTED>" in rendered
    assert "eyJ" not in rendered


def test_filter_idempotent():
    f = IdTokenScrubber()
    url = "idToken=eyJabc.def.ghi"
    r1 = _mk_record(url)
    f.filter(r1)
    r2 = _mk_record(r1.msg)
    f.filter(r2)
    assert r2.msg == r1.msg  # second pass changes nothing further


def test_filter_passes_through_lines_without_tokens():
    f = IdTokenScrubber()
    r = _mk_record("hello world")
    assert f.filter(r) is True
    assert r.msg == "hello world"


def test_install_is_idempotent():
    # Multiple installs don't stack filters uncontrollably.
    install_id_token_scrubber()
    install_id_token_scrubber()
    install_id_token_scrubber()
    uvicorn_access = logging.getLogger("uvicorn.access")
    scrubbers = [f for f in uvicorn_access.filters if isinstance(f, IdTokenScrubber)]
    # Idempotent install → one or zero global scrubber instances attached
    # (we don't care about root propagation vs direct attach; just that
    # the install guard prevents unbounded stacking).
    assert len(scrubbers) <= 1


def test_filter_handles_dict_args():
    # LogRecord accepts a 1-tuple containing a dict for %%(name)s formatting.
    f = IdTokenScrubber()
    r = _mk_record("some %(url)s event", args=({"url": "idToken=eyJabc"},))
    f.filter(r)
    assert "idToken=<REDACTED>" in r.getMessage()


# ---- F2 follow-up v2: hostToken + alias coverage ---------------------------


def test_scrub_strips_hosttoken():
    url = "WebSocket /ws/translate?orgId=x&hostToken=HT_RAW_SECRET_123"
    out = _scrub(url)
    assert out is not None
    assert "hostToken=<REDACTED>" in out
    assert "HT_RAW_SECRET_123" not in out


def test_scrub_strips_host_token_underscore_alias():
    url = "WebSocket /ws/translate?orgId=x&host_token=HT_RAW_SECRET_123"
    out = _scrub(url)
    assert out is not None
    assert "host_token=<REDACTED>" in out
    assert "HT_RAW_SECRET_123" not in out


def test_scrub_strips_token_alias():
    """`?token=` is an accepted alias for hostToken in main.py query-param reader."""
    url = "WebSocket /ws/translate?orgId=x&token=HT_RAW_SECRET_123"
    out = _scrub(url)
    assert out is not None
    assert "token=<REDACTED>" in out
    assert "HT_RAW_SECRET_123" not in out


def test_scrub_handles_combined_idtoken_and_hosttoken():
    """URL with BOTH credentials — both must be scrubbed in a single pass."""
    url = "WebSocket /ws/translate?orgId=x&idToken=eyJabc.def.ghi&hostToken=HT_SECRET"
    out = _scrub(url)
    assert out is not None
    assert "idToken=<REDACTED>" in out
    assert "hostToken=<REDACTED>" in out
    assert "eyJabc" not in out
    assert "HT_SECRET" not in out


# ---------------------------------------------------------------------
# v4: handler-level installation covers descendant loggers
# ---------------------------------------------------------------------

import io
import pytest
from app.access_log_filter import _uninstall_id_token_scrubber_for_tests


@pytest.fixture
def fresh_scrubber():
    """Install the scrubber on a clean logger tree, then uninstall after test."""
    _uninstall_id_token_scrubber_for_tests()
    install_id_token_scrubber()
    yield
    _uninstall_id_token_scrubber_for_tests()


def test_scrubber_covers_descendant_logger_propagation(fresh_scrubber):
    """A record emitted via a DESCENDANT logger propagates up to the root
    handler. The scrubber must redact the credential even though the record
    was never emitted directly through the root logger."""
    # Attach a StringIO handler to the ROOT logger AFTER install, to verify
    # the monkey-patched addHandler also installed the filter.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        descendant = logging.getLogger("app.descendant.deepdescendant")
        descendant.setLevel(logging.DEBUG)
        descendant.info("GET /ws/translate?idToken=eyJLEAK_BYTES HTTP/1.1")
        handler.flush()
        output = buf.getvalue()
        assert "idToken=<REDACTED>" in output, f"expected redaction in {output!r}"
        assert "eyJLEAK_BYTES" not in output, f"credential leaked in {output!r}"
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)


def test_scrubber_covers_uvicorn_access_logger(fresh_scrubber):
    """Existing behavior preserved: records emitted directly on uvicorn.access
    (not through a descendant) are also scrubbed."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.setLevel(logging.DEBUG)
    # Prevent propagation to avoid double-logging via the root handler too
    prev_propagate = uvicorn_access.propagate
    uvicorn_access.propagate = False
    uvicorn_access.addHandler(handler)
    try:
        uvicorn_access.info("GET /ws/translate?hostToken=HTLEAK HTTP/1.1")
        handler.flush()
        output = buf.getvalue()
        assert "hostToken=<REDACTED>" in output
        assert "HTLEAK" not in output
    finally:
        uvicorn_access.removeHandler(handler)
        uvicorn_access.propagate = prev_propagate


def test_scrubber_covers_handler_added_after_install(fresh_scrubber):
    """A handler added AFTER install (via logger.addHandler) must still get
    the scrubber, via the install's monkey-patch of addHandler."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    # Add the handler now, after install — the monkey-patched addHandler
    # must install the scrubber onto it.
    root.addHandler(handler)
    try:
        # Verify the handler has the scrubber attached.
        assert any(isinstance(f, IdTokenScrubber) for f in handler.filters), (
            "monkey-patched addHandler did not install scrubber on new handler"
        )
        # And that it actually redacts.
        deep = logging.getLogger("app.something.else")
        deep.setLevel(logging.DEBUG)
        deep.info("post-install: idToken=eyJLATE_LEAK")
        handler.flush()
        output = buf.getvalue()
        assert "idToken=<REDACTED>" in output
        assert "eyJLATE_LEAK" not in output
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)


def test_scrubber_covers_pre_install_handler_on_arbitrary_logger():
    """v5 fix: a handler attached to an arbitrary (non-root, non-uvicorn)
    logger BEFORE `install_id_token_scrubber()` runs must also get the
    filter. v4 only walked root + the uvicorn/fastapi named loggers;
    this case was out of scope. v5 iterates
    `logging.Logger.manager.loggerDict`.
    """
    _uninstall_id_token_scrubber_for_tests()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    custom_logger = logging.getLogger("app.pre_install_test.deep")
    custom_logger.setLevel(logging.DEBUG)
    # Prevent propagation so we observe exclusively this handler's output.
    prev_propagate = custom_logger.propagate
    custom_logger.propagate = False
    custom_logger.addHandler(handler)
    try:
        # Install AFTER the handler is already attached to a non-root,
        # non-well-known logger.
        install_id_token_scrubber()
        custom_logger.info(
            "GET /ws/translate?idToken=eyJPRE_INSTALL_LEAK&hostToken=HT_PRE HTTP/1.1"
        )
        handler.flush()
        output = buf.getvalue()
        assert "idToken=<REDACTED>" in output, (
            f"v5 pre-install coverage missing: {output!r}"
        )
        assert "hostToken=<REDACTED>" in output, (
            f"v5 pre-install coverage missing hostToken: {output!r}"
        )
        assert "eyJPRE_INSTALL_LEAK" not in output
        assert "HT_PRE" not in output
    finally:
        custom_logger.removeHandler(handler)
        custom_logger.propagate = prev_propagate
        _uninstall_id_token_scrubber_for_tests()
