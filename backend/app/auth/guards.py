from __future__ import annotations

from typing import Any, Iterable

from fastapi import HTTPException

from app.auth.firebase_auth import AuthenticatedUser


def _normalize_roles(roles: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for role in roles:
        token = str(role or "").strip().lower()
        if token:
            out.add(token)
    return out


def _clean_token(raw: object) -> str:
    return str(raw or "").strip()


def require_org_role(
    *,
    org_id: str,
    user: AuthenticatedUser,
    roles: Iterable[str],
    store: Any,
    missing_membership_detail: str = "org_access_denied",
) -> dict[str, Any]:
    clean_org_id = _clean_token(org_id)
    if not clean_org_id:
        raise HTTPException(status_code=404, detail="org_not_found")

    allowed_roles = _normalize_roles(roles)
    memberships = store.list_memberships(user.uid)
    match = next((row for row in memberships if _clean_token(row.get("orgId")) == clean_org_id), None)
    if not match:
        raise HTTPException(status_code=403, detail=missing_membership_detail)

    role = _clean_token(match.get("role")).lower()
    if allowed_roles and role not in allowed_roles:
        raise HTTPException(status_code=403, detail="forbidden")
    return dict(match)


def resolve_default_org_id_for_roles(
    *,
    user: AuthenticatedUser,
    roles: Iterable[str],
    store: Any,
) -> str:
    allowed_roles = _normalize_roles(roles)
    current_org_id = _clean_token(store.get_current_org_id(user.uid))
    if current_org_id:
        try:
            require_org_role(
                org_id=current_org_id,
                user=user,
                roles=allowed_roles,
                store=store,
            )
            return current_org_id
        except HTTPException as exc:
            if exc.status_code not in {403, 404}:
                raise

    memberships = store.list_memberships(user.uid)
    for row in memberships:
        org_id = _clean_token(row.get("orgId"))
        role = _clean_token(row.get("role")).lower()
        if org_id and (not allowed_roles or role in allowed_roles):
            return org_id

    if memberships:
        raise HTTPException(status_code=403, detail="forbidden")
    raise HTTPException(status_code=404, detail="org_not_found")
