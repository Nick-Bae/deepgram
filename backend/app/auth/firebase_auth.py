from __future__ import annotations

from functools import lru_cache
import os
from threading import Lock
import time
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
    isSuper: bool = False


_SUPER_CLAIM_KEYS: tuple[str, ...] = (
    "super_admin",
    "superAdmin",
    "is_super_user",
    "isSuperUser",
    "isMaster",
    "master",
)
_SUPER_UID_CACHE_TTL_SECONDS = max(0, int((os.getenv("AUTH_SUPER_UID_CACHE_TTL_SECONDS") or "900").strip() or "900"))
_ID_TOKEN_CLOCK_SKEW_SECONDS = max(
    0,
    min(
        300,
        int((os.getenv("AUTH_ID_TOKEN_CLOCK_SKEW_SECONDS") or "60").strip() or "60"),
    ),
)
_super_uid_cache: dict[str, tuple[float, bool]] = {}
_super_uid_lock = Lock()


def _claim_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _decode_super_claim(decoded: dict) -> bool:
    for key in _SUPER_CLAIM_KEYS:
        if key in decoded and _claim_bool(decoded.get(key)):
            return True
    claims = decoded.get("claims")
    if isinstance(claims, dict):
        for key in _SUPER_CLAIM_KEYS:
            if key in claims and _claim_bool(claims.get(key)):
                return True
    return False


def _cache_super_uid(uid: str, is_super: bool) -> None:
    clean_uid = str(uid or "").strip()
    if not clean_uid:
        return
    if _SUPER_UID_CACHE_TTL_SECONDS <= 0:
        return
    with _super_uid_lock:
        _super_uid_cache[clean_uid] = (time.monotonic() + _SUPER_UID_CACHE_TTL_SECONDS, bool(is_super))
        if len(_super_uid_cache) > 10000:
            cutoff = time.monotonic()
            stale = [k for k, (expires_at, _) in _super_uid_cache.items() if expires_at <= cutoff]
            for key in stale:
                _super_uid_cache.pop(key, None)


def is_super_uid(uid: Optional[str]) -> bool:
    clean_uid = str(uid or "").strip()
    if not clean_uid:
        return False
    with _super_uid_lock:
        row = _super_uid_cache.get(clean_uid)
        if not row:
            return False
        expires_at, is_super = row
        if expires_at <= time.monotonic():
            _super_uid_cache.pop(clean_uid, None)
            return False
        return bool(is_super)


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
        decoded = firebase_auth.verify_id_token(  # type: ignore[union-attr]
            token,
            clock_skew_seconds=_ID_TOKEN_CLOCK_SKEW_SECONDS,
        )
    except Exception as exc:
        err_name = type(exc).__name__
        err_msg = str(exc).lower()
        if "certificate" in err_name.lower() or "transport" in err_name.lower() or "connection" in err_msg:
            raise HTTPException(status_code=503, detail="auth_provider_unavailable") from exc
        raise HTTPException(status_code=401, detail="invalid_id_token") from exc
    uid = str(decoded.get("uid") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="invalid_id_token")
    sign_in_provider = ""
    firebase_meta = decoded.get("firebase")
    if isinstance(firebase_meta, dict):
        sign_in_provider = str(firebase_meta.get("sign_in_provider") or "").strip().lower()
    if sign_in_provider == "anonymous":
        raise HTTPException(status_code=401, detail="anonymous_auth_disabled")
    email_raw = decoded.get("email")
    name_raw = decoded.get("name")
    is_super = _decode_super_claim(decoded)
    _cache_super_uid(uid, is_super)
    return AuthenticatedUser(
        uid=uid,
        email=str(email_raw).strip() if email_raw else None,
        displayName=str(name_raw).strip() if name_raw else None,
        isSuper=is_super,
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
