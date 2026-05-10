"""Search page route. Python 3.8 compatible.

Uses SQL LIKE for speed, falls back to Python substring for Cyrillic.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_current_user
from app.models.anime import Anime
from app.models.user import User
from database import get_session
from main_templates import templates

router = APIRouter()


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
    user: Optional[User] = Depends(get_current_user),
):
    q_raw = q.strip()
    q_low = q_raw.lower()
    anime_list: List[Anime] = []

    if q_low:
        try:
            pattern = "%{}%".format(q_low)
            result = await session.execute(
                select(Anime)
                .where(
                    or_(
                        func.lower(Anime.title).like(pattern),
                        func.lower(Anime.alternative_titles).like(pattern),
                    )
                )
                .order_by(Anime.rating.desc(), Anime.id.desc())
                .limit(120)
            )
            anime_list = list(result.scalars().all())

            # Cyrillic fallback: if SQL missed results, scan remaining rows
            if len(anime_list) < 60:
                seen_ids = {a.id for a in anime_list}
                extra = await session.execute(
                    select(Anime)
                    .where(Anime.id.notin_(seen_ids))
                    .order_by(Anime.rating.desc())
                    .limit(3000)
                )
                for a in extra.scalars().all():
                    if len(anime_list) >= 120:
                        break
                    haystack = "{} {}".format(a.title or "", a.alternative_titles or "").lower()
                    if q_low in haystack:
                        anime_list.append(a)
        except Exception:
            anime_list = []

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "anime_list": anime_list,
            "user": user,
            "active": "search",
            "page_title": "Поиск: {}".format(q_raw) if q_raw else "Поиск",
            "search_query": q_raw,
            "pagination": None,
        },
    )
