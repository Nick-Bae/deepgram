"""E2E test bootstrap: substitute Firebase token verification with a
fixed test identity, then launch uvicorn.

This module lives entirely under `backend/tests/`. Production code
(`backend/app/`) is imported and used unchanged — the real routes,
`authorize_host`, `multichurch_store`, Firestore client, Redis
pubsub, and cleanup paths all run. Only `firebase_auth.
verify_id_token_value` is monkey-patched in-process, BEFORE uvicorn
serves its first request, so no HTTP handler ever sees the real
Firebase Admin SDK path in the harness.

Configuration is read from the harness's own env vars, never from
production-recognised names:

  E2E_STUB_AUTH_MAPPING
      JSON object mapping bearer tokens to uids. Example:
      `{"host-token": "e2e-host-uid", "outsider-token": "e2e-outsider-uid"}`
      Any bearer token NOT present in the mapping is rejected. Any
      request with a missing/empty Authorization header is rejected.

  E2E_UVICORN_HOST, E2E_UVICORN_PORT
      Bind address for uvicorn. Required.

  E2E_UVICORN_APP
      Import path for the FastAPI app. Defaults to `app.main:app`.

Neither env var name overlaps with anything the shipped app reads.
The production `firebase_auth` module has no knowledge of these
names.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional


def _load_mapping() -> dict:
    raw = (os.environ.get("E2E_STUB_AUTH_MAPPING") or "").strip()
    if not raw:
        raise RuntimeError("E2E_STUB_AUTH_MAPPING is required (JSON dict)")
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"E2E_STUB_AUTH_MAPPING is not valid JSON: {exc}")
    if not isinstance(mapping, dict) or not mapping:
        raise RuntimeError("E2E_STUB_AUTH_MAPPING must be a non-empty JSON dict")
    for k, v in mapping.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k or not v:
            raise RuntimeError("E2E_STUB_AUTH_MAPPING keys and values must be non-empty strings")
    return mapping


def _install_stub_auth(mapping: dict) -> None:
    """Replace verify_id_token_value with a stub that recognises
    tokens from the mapping and returns AuthenticatedUser(uid=...)
    with no super claim, no email, no display name. Every other
    token is rejected."""
    from app.auth import firebase_auth as _fa

    # Constant-time compare against every entry so a single lookup
    # miss cannot be timed to enumerate valid tokens.
    from hmac import compare_digest as _cd
    token_pairs = tuple(
        (t.encode("utf-8"), uid) for t, uid in mapping.items()
    )

    def _stub_verify(id_token):
        token = (id_token or "").strip()
        if not token:
            return None
        token_bytes = token.encode("utf-8")
        for expected_bytes, uid in token_pairs:
            if _cd(token_bytes, expected_bytes):
                return _fa.AuthenticatedUser(
                    uid=uid,
                    email=None,
                    displayName=None,
                    isSuper=False,
                )
        return None

    _fa.verify_id_token_value = _stub_verify


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("E2E_UVICORN_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("E2E_UVICORN_PORT", "0") or "0"))
    parser.add_argument("--app", default=os.environ.get("E2E_UVICORN_APP", "app.main:app"))
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    if args.port <= 0:
        raise SystemExit("--port must be > 0 (E2E_UVICORN_PORT or --port)")

    mapping = _load_mapping()
    _install_stub_auth(mapping)

    # Sanity: the monkey-patch must be in place before the FastAPI
    # app imports finish binding routes to the real dependency. We
    # patched `verify_id_token_value`; the dependency chain is
    # `get_current_user_required` → `get_current_user_optional` →
    # `verify_bearer_token` → `verify_id_token_value`, all resolved
    # at call time — so patching the innermost function is sufficient
    # regardless of import order.

    import uvicorn
    uvicorn.run(
        args.app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
