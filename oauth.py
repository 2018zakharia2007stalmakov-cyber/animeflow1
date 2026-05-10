"""OAuth 2.0 login: Google + VK. Python 3.8 compatible.

Requires env vars:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
  VK_CLIENT_ID,     VK_CLIENT_SECRET
  APP_BASE_URL  (e.g. https://yourapp.onrender.com)

Buttons still appear without env vars; clicking shows a friendly error page.
"""
import logging
import os
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_password
from app.models.user import User
from database import get_session
from main_templates import templates

logger = logging.getLogger("animeflow.oauth")
router = APIRouter(prefix="/auth")

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_VK_AUTH_URL = "https://oauth.vk.com/authorize"
_VK_TOKEN_URL = "https://oauth.vk.com/access_token"
_VK_USER_URL = "https://api.vk.com/method/users.get"


def _base_url(request: Request) -> str:
    base = os.environ.get("APP_BASE_URL", "").rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return base


def _not_configured(request: Request, provider: str):
    return templates.TemplateResponse(
        request,
        "404.html",
        {
            "user": None,
            "active": "",
            "error_code": 503,
            "error_message": f"OAuth через {provider} не настроен. Войдите по логину и паролю.",
        },
        status_code=503,
    )


# ── Google ──────────────────────────────────────────────────────────────────

@router.get("/google")
async def google_login(request: Request):
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not client_id:
        return _not_configured(request, "Google")
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    redirect_uri = _base_url(request) + "/auth/google/callback"
    params = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    })
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{params}", status_code=302)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    session: AsyncSession = Depends(get_session),
):
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return _not_configured(request, "Google")

    stored_state = request.session.pop("oauth_state", "")
    if state != stored_state:
        return RedirectResponse("/login", status_code=303)

    redirect_uri = _base_url(request) + "/auth/google/callback"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.post(_GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            })
            token_data = token_resp.json()
            access_token = token_data.get("access_token", "")
            if not access_token:
                raise ValueError("no access_token")

            info_resp = await client.get(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            info = info_resp.json()
    except Exception as exc:
        logger.error("google_callback error: %s", exc)
        return RedirectResponse("/login", status_code=303)

    google_id = str(info.get("sub", ""))
    email = str(info.get("email", ""))
    name = str(info.get("name", "") or info.get("given_name", "") or "user")

    if not google_id or not email:
        return RedirectResponse("/login", status_code=303)

    # Find or create user
    user = None
    result = await session.execute(select(User).where(User.google_id == google_id))
    user = result.scalar_one_or_none()

    if not user:
        result2 = await session.execute(select(User).where(User.email == email))
        user = result2.scalar_one_or_none()
        if user:
            user.google_id = google_id
        else:
            from sqlalchemy import func
            count = await session.scalar(select(func.count()).select_from(User))
            role = "admin" if (count or 0) == 0 else "user"
            safe_name = name[:30].replace(" ", "_")
            existing_name = await session.scalar(
                select(User.username).where(User.username == safe_name)
            )
            if existing_name:
                safe_name = f"{safe_name}_{google_id[:6]}"
            user = User(
                username=safe_name,
                email=email,
                password_hash=hash_password(secrets.token_hex(16)),
                role=role,
                google_id=google_id,
                avatar_url=info.get("picture"),
            )
            session.add(user)

    await session.commit()
    await session.refresh(user)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


# ── VK ──────────────────────────────────────────────────────────────────────

@router.get("/vk")
async def vk_login(request: Request):
    client_id = os.environ.get("VK_CLIENT_ID", "")
    if not client_id:
        return _not_configured(request, "ВКонтакте")
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    redirect_uri = _base_url(request) + "/auth/vk/callback"
    params = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "email",
        "state": state,
        "v": "5.131",
        "display": "page",
    })
    return RedirectResponse(f"{_VK_AUTH_URL}?{params}", status_code=302)


@router.get("/vk/callback")
async def vk_callback(
    request: Request,
    code: str = "",
    state: str = "",
    session: AsyncSession = Depends(get_session),
):
    client_id = os.environ.get("VK_CLIENT_ID", "")
    client_secret = os.environ.get("VK_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return _not_configured(request, "ВКонтакте")

    stored_state = request.session.pop("oauth_state", "")
    if state != stored_state:
        return RedirectResponse("/login", status_code=303)

    redirect_uri = _base_url(request) + "/auth/vk/callback"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.get(_VK_TOKEN_URL, params={
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            })
            token_data = token_resp.json()
            access_token = token_data.get("access_token", "")
            vk_id = str(token_data.get("user_id", ""))
            vk_email = str(token_data.get("email", ""))
            if not access_token or not vk_id:
                raise ValueError("no access_token/user_id")

            user_resp = await client.get(_VK_USER_URL, params={
                "user_ids": vk_id,
                "fields": "photo_200,screen_name",
                "access_token": access_token,
                "v": "5.131",
            })
            user_data = (user_resp.json().get("response") or [{}])[0]
    except Exception as exc:
        logger.error("vk_callback error: %s", exc)
        return RedirectResponse("/login", status_code=303)

    first = str(user_data.get("first_name", "user"))
    last = str(user_data.get("last_name", ""))
    screen = str(user_data.get("screen_name", f"vk_{vk_id}"))
    avatar = user_data.get("photo_200")

    # Find or create user
    result = await session.execute(select(User).where(User.vk_id == vk_id))
    user = result.scalar_one_or_none()

    if not user and vk_email:
        result2 = await session.execute(select(User).where(User.email == vk_email))
        user = result2.scalar_one_or_none()
        if user:
            user.vk_id = vk_id

    if not user:
        from sqlalchemy import func
        count = await session.scalar(select(func.count()).select_from(User))
        role = "admin" if (count or 0) == 0 else "user"
        safe_name = screen[:30]
        existing = await session.scalar(select(User.username).where(User.username == safe_name))
        if existing:
            safe_name = f"{safe_name}_{vk_id[:6]}"
        email_fallback = vk_email or f"vk_{vk_id}@vk.placeholder"
        user = User(
            username=safe_name,
            email=email_fallback,
            password_hash=hash_password(secrets.token_hex(16)),
            role=role,
            vk_id=vk_id,
            avatar_url=avatar,
        )
        session.add(user)

    await session.commit()
    await session.refresh(user)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)
