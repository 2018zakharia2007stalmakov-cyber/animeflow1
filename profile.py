"""Profile page, avatar upload, settings. Python 3.8 compatible."""
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_current_user, hash_password, verify_password
from app.models.anime import Anime
from app.models.user import Favorite, User, WatchProgress
from database import get_session
from main_templates import templates

logger = logging.getLogger("animeflow.profile")
router = APIRouter()

_MAX_AVATAR_BYTES = 4 * 1024 * 1024
_ALLOWED_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}

AVATAR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "app", "static", "avatars",
)


def _ensure_avatar_dir():
    os.makedirs(AVATAR_DIR, exist_ok=True)


async def _profile_ctx(user, session):
    fav_q = await session.execute(
        select(Anime)
        .join(Favorite, Favorite.anime_id == Anime.id)
        .where(Favorite.user_id == user.id)
        .order_by(Favorite.created_at.desc())
    )
    from app.models.achievement import UserAchievement
    ach_q = await session.execute(
        select(UserAchievement).where(
            UserAchievement.user_id == user.id,
            UserAchievement.unlocked == 1,
        )
    )
    return fav_q.scalars().all(), len(ach_q.scalars().all())


@router.get("/profile", response_class=HTMLResponse)
async def profile(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: Optional[User] = Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    favorites, unlocked_count = await _profile_ctx(user, session)
    err = request.query_params.get("err")
    error_msg = None
    if err == "type":
        error_msg = "Недопустимый формат файла. Используйте JPG, PNG или WebP."
    elif err == "size":
        error_msg = "Файл слишком большой (максимум 4 МБ)."
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "user": user,
            "favorites": favorites,
            "active": "profile",
            "unlocked_count": unlocked_count,
            "profile_error": error_msg,
            "profile_success": None,
        },
    )


@router.post("/profile/avatar")
async def upload_avatar(
    request: Request,
    avatar: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    user: Optional[User] = Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    content_type = (avatar.content_type or "").lower()
    if content_type not in _ALLOWED_TYPES:
        return RedirectResponse("/profile?err=type", status_code=303)
    data = await avatar.read(_MAX_AVATAR_BYTES + 1)
    if len(data) > _MAX_AVATAR_BYTES:
        return RedirectResponse("/profile?err=size", status_code=303)
    _ensure_avatar_dir()
    ext = "jpg" if ("jpeg" in content_type or "jpg" in content_type) else (
        "png" if "png" in content_type else (
        "gif" if "gif" in content_type else "webp"))
    filename = "{}.{}".format(user.id, ext)
    path = os.path.join(AVATAR_DIR, filename)
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        img.thumbnail((256, 256))
        out = BytesIO()
        img.save(out, format="JPEG" if ext == "jpg" else ext.upper(), quality=88)
        data = out.getvalue()
    except Exception:
        pass
    with open(path, "wb") as f:
        f.write(data)
    user.avatar_url = "/static/avatars/{}".format(filename)
    try:
        await session.commit()
    except Exception as exc:
        logger.error("avatar commit: %s", exc)
        await session.rollback()
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/settings")
async def update_settings(
    request: Request,
    bio: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: Optional[User] = Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    error = None
    success = None
    user.bio = (bio or "").strip()[:300]
    if new_password:
        if not current_password:
            error = "Введите текущий пароль"
        elif not verify_password(current_password, user.password_hash):
            error = "Текущий пароль неверен"
        elif len(new_password) < 6:
            error = "Новый пароль не короче 6 символов"
        else:
            user.password_hash = hash_password(new_password)
            success = "Пароль успешно изменён"
    if not error:
        success = success or "Настройки сохранены"
        try:
            await session.commit()
        except Exception as exc:
            logger.error("settings commit: %s", exc)
            await session.rollback()
            error = "Ошибка сохранения"
    favorites, unlocked_count = await _profile_ctx(user, session)
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "user": user,
            "favorites": favorites,
            "active": "profile",
            "unlocked_count": unlocked_count,
            "profile_error": error,
            "profile_success": success,
        },
    )
