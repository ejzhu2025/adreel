"""auth/router.py — /auth/* endpoints: login, callback, logout, me."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from web.auth.deps import COOKIE_NAME, JWT_EXPIRE_DAYS, create_token, current_user
from web.auth.google import exchange_code, get_authorization_url, get_userinfo
from web.auth.models import upsert_user

router = APIRouter(prefix="/auth", tags=["auth"])

# Simple in-process state nonce store (sufficient for single-instance deployment)
_pending_states: set[str] = set()


@router.get("/login")
def login():
    """Redirect browser to Google consent screen."""
    state = secrets.token_urlsafe(16)
    _pending_states.add(state)
    return RedirectResponse(get_authorization_url(state))


@router.get("/callback")
def callback(code: str = "", state: str = "", error: str = ""):
    """Google posts back here after user grants/denies consent."""
    if error:
        raise HTTPException(400, f"Google OAuth error: {error}")
    if state not in _pending_states:
        raise HTTPException(400, "Invalid or expired OAuth state")
    _pending_states.discard(state)

    try:
        tokens = exchange_code(code)
        info = get_userinfo(tokens["access_token"])
    except Exception as exc:
        raise HTTPException(502, f"Google API error: {exc}")

    user = upsert_user(
        google_id=info["sub"],
        email=info["email"],
        name=info.get("name", ""),
        picture=info.get("picture", ""),
    )

    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        key=COOKIE_NAME,
        value=create_token(user.id),
        httponly=True,
        samesite="lax",
        secure=False,           # flip to True behind HTTPS in production
        max_age=JWT_EXPIRE_DAYS * 86400,
    )
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/me")
def me(user=Depends(current_user)):
    """Return current user info (used by frontend to check login state)."""
    return user.to_dict()


# ── Guest access code ─────────────────────────────────────────────────────────

from web.routers.projects import GUEST_COOKIE, _GUEST_CODES  # noqa: E402


@router.get("/guest-access")
def guest_access_status(request: Request):
    """Check if the current request has a valid guest access code cookie."""
    code = request.cookies.get(GUEST_COOKIE, "")
    valid = bool(_GUEST_CODES) and code in _GUEST_CODES
    return {"valid": valid}


class GuestAccessRequest(BaseModel):
    code: str

from pydantic import BaseModel  # noqa: E402


@router.post("/guest-access")
def guest_access_submit(body: GuestAccessRequest):
    """Validate a guest access code and set a cookie if valid."""
    if not _GUEST_CODES or body.code.strip() not in _GUEST_CODES:
        raise HTTPException(status_code=403, detail="Invalid access code")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key=GUEST_COOKIE,
        value=body.code.strip(),
        httponly=True,
        samesite="lax",
        max_age=86400 * 30,  # 30 days
    )
    return resp
