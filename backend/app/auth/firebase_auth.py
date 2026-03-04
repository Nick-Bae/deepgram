from __future__ import annotations

from functools import lru_cache
import os
from typing import Optional

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel

try:
    import firebase_admin  # type: ignore
    from firebase_admin import auth as firebase_auth  # type: ignore
    from firebase_admin import credentials as firebase_credentials  # type: ignore
except Exception:  # pragma: no cover - optional dependency during early bootstrap
    firebase_admin = None
    firebase_auth = None
    firebase_credentials = None


class AuthenticatedUser(BaseModel):
    uid: str
    email: Optional[str] = None
    displayName: Optional[str] = None


def _project_id() -> Optional[str]:
    project = (
        (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
        or (os.getenv("GCP_PROJECT") or "").strip()
        or (os.getenv("FIREBASE_PROJECT_ID") or "").strip()
    )
    return project or None


@lru_cache(maxsize=1)
def _ensure_firebase_app() -> bool:
    if firebase_admin is None or firebase_auth is None or firebase_credentials is None:
        return False
    if firebase_admin._apps:  # type: ignore[attr-defined]
        return True

    cred_path = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if cred_path and os.path.exists(cred_path):
        cred = firebase_credentials.Certificate(cred_path)
    else:
        cred = firebase_credentials.ApplicationDefault()

    options = {}
    project = _project_id()
    if project:
        options["projectId"] = project

    firebase_admin.initialize_app(cred, options or None)
    return True


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    raw = (authorization or "").strip()
    if not raw:
        return None
    if not raw.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="invalid_authorization_header")
    token = raw[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return token


def verify_id_token_value(id_token: Optional[str]) -> Optional[AuthenticatedUser]:
    token = (id_token or "").strip()
    if not token:
        return None
    try:
        initialized = _ensure_firebase_app()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="firebase_init_failed") from exc
    if not initialized:
        raise HTTPException(status_code=503, detail="firebase_admin_not_configured")
    try:
        decoded = firebase_auth.verify_id_token(token)  # type: ignore[union-attr]
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid_id_token") from exc
    uid = str(decoded.get("uid") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="invalid_id_token")
    email_raw = decoded.get("email")
    name_raw = decoded.get("name")
    return AuthenticatedUser(
        uid=uid,
        email=str(email_raw).strip() if email_raw else None,
        displayName=str(name_raw).strip() if name_raw else None,
    )


def verify_bearer_token(authorization: Optional[str]) -> Optional[AuthenticatedUser]:
    token = _extract_bearer_token(authorization)
    return verify_id_token_value(token)


def get_current_user_optional(
    authorization: Optional[str] = Header(default=None),
) -> Optional[AuthenticatedUser]:
    return verify_bearer_token(authorization)


def get_current_user_required(
    user: Optional[AuthenticatedUser] = Depends(get_current_user_optional),
) -> AuthenticatedUser:
    if not user:
        raise HTTPException(status_code=401, detail="auth_required")
    return user
