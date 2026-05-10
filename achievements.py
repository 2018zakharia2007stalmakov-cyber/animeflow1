"""Achievement definitions and progress computation. Python 3.8 compatible."""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.achievement import UserAchievement

logger = logging.getLogger("animeflow.achievements")

C = "common"
R = "rare"
E = "epic"
L = "legendary"

RARITY_LABEL: Dict[str, str] = {
    C: "Обычное",
    R: "Редкое",
    E: "Эпическое",
    L: "Легендарное",
}
RARITY_CSS: Dict[str, str] = {
    C: "bg-gray-500/15 border-gray-500/30 text-gray-300",
    R: "bg-blue-500/15 border-blue-500/30 text-blue-300",
    E: "bg-violet-500/15 border-violet-500/30 text-violet-300",
    L: "bg-yellow-500/15 border-yellow-500/30 text-yellow-300",
}
RARITY_GLOW: Dict[str, str] = {
    C: "",
    R: "0 0 18px -5px rgba(59,130,246,0.55)",
    E: "0 0 18px -5px rgba(139,92,246,0.65)",
    L: "0 0 22px -5px rgba(234,179,8,0.65)",
}

ACHIEVEMENTS: List[Dict[str, Any]] = [
    {"key": "welcome",           "title": "Добро пожаловать",     "desc": "Создать аккаунт на AnimeFlow",            "icon": "🎉", "rarity": C, "max": 1},
    {"key": "first_watch",       "title": "Первый просмотр",      "desc": "Посмотреть первую серию",                 "icon": "▶️",  "rarity": C, "max": 1},
    {"key": "first_hours",       "title": "Первые часы",          "desc": "Провести 1 час за просмотром аниме",      "icon": "⏱️",  "rarity": C, "max": 3600},
    {"key": "first_season",      "title": "Первый сезон",         "desc": "Полностью посмотреть один сезон аниме",   "icon": "🏆", "rarity": R, "max": 1},
    {"key": "comeback",          "title": "Возвращение",          "desc": "Заходить на сайт 3 дня подряд",           "icon": "🔄", "rarity": C, "max": 3},
    {"key": "night_marathon",    "title": "Ночной марафон",       "desc": "Смотреть аниме после 2:00 ночи",          "icon": "🌙", "rarity": R, "max": 1},
    {"key": "marathoner",        "title": "Марафонец",            "desc": "Посмотреть 10 серий подряд за день",      "icon": "🏃", "rarity": R, "max": 10},
    {"key": "no_stop",           "title": "Без остановки",        "desc": "Смотреть 3 часа подряд",                  "icon": "⚡", "rarity": E, "max": 3},
    {"key": "hundred_eps",       "title": "Сотня серий",          "desc": "Посмотреть 100 серий",                    "icon": "💯", "rarity": R, "max": 100},
    {"key": "do_you_sleep",      "title": "Ты вообще спишь?",     "desc": "Посмотреть 500 серий",                    "icon": "😴", "rarity": E, "max": 500},
    {"key": "romantic",          "title": "Романтик",             "desc": "Посмотреть 20 романтических аниме",       "icon": "💕", "rarity": R, "max": 20},
    {"key": "shonen",            "title": "Шинен-машина",         "desc": "Посмотреть 10 сёнен-аниме",               "icon": "⚔️",  "rarity": R, "max": 10},
    {"key": "isekai_king",       "title": "Король иссекаев",      "desc": "Посмотреть 10 исекай-аниме",              "icon": "🌀", "rarity": E, "max": 10},
    {"key": "horror",            "title": "Хоррор внутри тебя",  "desc": "Посмотреть 10 хоррор-аниме",              "icon": "👻", "rarity": R, "max": 10},
    {"key": "regular",           "title": "Постоянный зритель",   "desc": "Заходить на сайт 7 дней подряд",          "icon": "📅", "rarity": R, "max": 7},
    {"key": "legend",            "title": "Легенда сайта",        "desc": "Заходить на сайт 30 дней подряд",         "icon": "👑", "rarity": L, "max": 30},
    {"key": "veteran",           "title": "Ветеран AnimeFlow",    "desc": "Аккаунту исполнилось 100 дней",           "icon": "🎖️",  "rarity": E, "max": 100},
    {"key": "collector",         "title": "Коллекционер",         "desc": "Добавить 50 аниме в избранное",           "icon": "❤️",  "rarity": R, "max": 50},
    {"key": "explorer",          "title": "Исследователь",        "desc": "Открыть 100 разных тайтлов",              "icon": "🔭", "rarity": E, "max": 100},
    {"key": "random_fate",       "title": "Случайность судьбы",   "desc": "Открыть случайное аниме 10 раз",          "icon": "🎲", "rarity": R, "max": 10},
    {"key": "old_school",        "title": "Олд",                  "desc": "Посмотреть аниме 90-х годов",             "icon": "📼", "rarity": R, "max": 1},
    {"key": "culture",           "title": "Человек культуры",     "desc": "Посмотреть аниме жанра этти/18+",         "icon": "🎌", "rarity": E, "max": 1},
    {"key": "absolute_otaku",    "title": "Абсолютный отаку",     "desc": "1000 часов просмотра аниме",              "icon": "🌟", "rarity": L, "max": 1000},
    {"key": "legendary_marathon","title": "Легендарный марафон",  "desc": "Посмотреть 24 серии за сутки",            "icon": "🔥", "rarity": L, "max": 24},
    {"key": "elite",             "title": "AnimeFlow Elite",      "desc": "Получить 20 достижений",                  "icon": "💎", "rarity": L, "max": 20},
]

ACHIEVEMENT_MAP: Dict[str, Dict[str, Any]] = {a["key"]: a for a in ACHIEVEMENTS}

# Keys that are incremented via events, not computed from DB
_EVENT_KEYS = {"random_fate", "night_marathon", "marathoner", "no_stop", "first_season",
               "legendary_marathon", "old_school", "culture", "first_watch"}


async def check_and_unlock(
    user_id: int,
    session: AsyncSession,
    event: str = "",
    event_value: float = 1.0,
) -> List[Dict[str, Any]]:
    """Compute/increment achievement progress and return newly unlocked list."""
    from app.models.anime import Anime, Episode
    from app.models.user import Favorite, User, WatchProgress

    user = await session.get(User, user_id)
    if not user:
        return []

    rows = await session.execute(
        select(UserAchievement).where(UserAchievement.user_id == user_id)
    )
    ua_map: Dict[str, UserAchievement] = {ua.achievement_key: ua for ua in rows.scalars().all()}

    def _ua(key: str) -> UserAchievement:
        if key not in ua_map:
            obj = UserAchievement(user_id=user_id, achievement_key=key, progress=0.0, unlocked=0)
            session.add(obj)
            ua_map[key] = obj
        return ua_map[key]

    # --- DB-computed metrics ---
    ep_count: int = (await session.scalar(
        select(func.count()).select_from(WatchProgress)
        .where(WatchProgress.user_id == user_id, WatchProgress.timestamp > 30)
    )) or 0

    total_secs: float = float((await session.scalar(
        select(func.sum(WatchProgress.timestamp)).where(WatchProgress.user_id == user_id)
    )) or 0)

    fav_count: int = (await session.scalar(
        select(func.count()).select_from(Favorite).where(Favorite.user_id == user_id)
    )) or 0

    # Count unique anime titles the user has watched (for explorer achievement)
    unique_anime_count: int = (await session.scalar(
        select(func.count(func.distinct(Episode.anime_id)))
        .select_from(WatchProgress)
        .join(Episode, Episode.id == WatchProgress.episode_id)
        .where(WatchProgress.user_id == user_id, WatchProgress.timestamp > 30)
    )) or 0

    account_days: int = max(0, (datetime.utcnow() - user.created_at).days)
    login_streak: int = getattr(user, "login_streak", 0) or 0

    # Genre counts from watched anime
    watched_q = await session.execute(
        select(Anime)
        .join(Episode, Episode.anime_id == Anime.id)
        .join(WatchProgress, WatchProgress.episode_id == Episode.id)
        .where(WatchProgress.user_id == user_id, WatchProgress.timestamp > 30)
        .distinct()
    )
    watched_anime = watched_q.scalars().all()

    def _has_genre(anime: Anime, *terms: str) -> bool:
        g = (anime.genres or "").lower()
        return any(t in g for t in terms)

    romantic_c = sum(1 for a in watched_anime if _has_genre(a, "романтика", "romance", "shoujo", "сёдзё"))
    shonen_c   = sum(1 for a in watched_anime if _has_genre(a, "сёнен", "shonen", "shounen"))
    isekai_c   = sum(1 for a in watched_anime if _has_genre(a, "исекай", "isekai"))
    horror_c   = sum(1 for a in watched_anime if _has_genre(a, "ужасы", "horror", "хоррор"))

    unlocked_before = sum(1 for ua in ua_map.values() if ua.unlocked)

    computed_progress: Dict[str, float] = {
        "welcome":        1.0,
        "first_watch":    min(1.0, float(ep_count)),
        "first_hours":    min(3600.0, total_secs),
        "hundred_eps":    min(100.0, float(ep_count)),
        "do_you_sleep":   min(500.0, float(ep_count)),
        "collector":      min(50.0, float(fav_count)),
        "veteran":        min(100.0, float(account_days)),
        "comeback":       min(3.0, float(login_streak)),
        "regular":        min(7.0, float(login_streak)),
        "legend":         min(30.0, float(login_streak)),
        "romantic":       min(20.0, float(romantic_c)),
        "shonen":         min(10.0, float(shonen_c)),
        "isekai_king":    min(10.0, float(isekai_c)),
        "horror":         min(10.0, float(horror_c)),
        "absolute_otaku": min(1000.0, total_secs / 3600.0),
        "explorer":       min(100.0, float(unique_anime_count)),
    }

    # --- Event-based increments ---
    event_key_map: Dict[str, str] = {
        "random_opened":      "random_fate",
        "night_watch":        "night_marathon",
        "episode_in_session": "marathoner",
        "hours_in_session":   "no_stop",
        "season_completed":   "first_season",
        "day_episodes":       "legendary_marathon",
        "old_school_watched": "old_school",
        "culture_watched":    "culture",
        "episode_watched":    "first_watch",
    }

    now = datetime.utcnow()
    newly_unlocked: List[Dict[str, Any]] = []

    # Apply computed progress
    for key, prog in computed_progress.items():
        ach = ACHIEVEMENT_MAP.get(key)
        if not ach:
            continue
        ua = _ua(key)
        if ua.unlocked:
            continue
        ua.progress = prog
        ua.updated_at = now
        if prog >= ach["max"]:
            ua.unlocked = 1
            ua.unlocked_at = now
            newly_unlocked.append({"key": key, "title": ach["title"], "icon": ach["icon"], "rarity": ach["rarity"], "desc": ach["desc"]})

    # Apply event-based increment
    if event and event in event_key_map:
        key = event_key_map[event]
        ach = ACHIEVEMENT_MAP.get(key)
        if ach:
            ua = _ua(key)
            if not ua.unlocked:
                ua.progress = min(float(ach["max"]), ua.progress + event_value)
                ua.updated_at = now
                if ua.progress >= ach["max"]:
                    ua.unlocked = 1
                    ua.unlocked_at = now
                    newly_unlocked.append({"key": key, "title": ach["title"], "icon": ach["icon"], "rarity": ach["rarity"], "desc": ach["desc"]})

    # Elite: based on total unlocked (including this batch)
    unlocked_now = unlocked_before + len(newly_unlocked)
    elite_ua = _ua("elite")
    if not elite_ua.unlocked:
        elite_ua.progress = min(20.0, float(unlocked_now))
        elite_ua.updated_at = now
        if elite_ua.progress >= 20:
            elite_ua.unlocked = 1
            elite_ua.unlocked_at = now
            newly_unlocked.append({"key": "elite", "title": "AnimeFlow Elite", "icon": "💎", "rarity": L, "desc": "Получить 20 достижений"})

    try:
        await session.commit()
    except Exception as exc:
        logger.error("achievement commit failed: %s", exc)
        await session.rollback()

    return newly_unlocked


async def update_login_streak(user_id: int, session: AsyncSession) -> None:
    """Update login streak for user. Call on every login."""
    from app.models.user import User
    user = await session.get(User, user_id)
    if not user:
        return
    now = datetime.utcnow()
    last = getattr(user, "last_login_at", None)
    streak = getattr(user, "login_streak", 0) or 0
    if last is None:
        streak = 1
    else:
        delta = (now.date() - last.date()).days
        if delta == 1:
            streak += 1
        elif delta > 1:
            streak = 1
        # delta == 0: same day, keep streak
    user.login_streak = streak
    user.last_login_at = now
    try:
        await session.commit()
    except Exception:
        await session.rollback()


async def backfill_achievements_for_all_users(session: AsyncSession) -> None:
    """
    Recalculate achievements for ALL existing users based on their watch history.
    Safe to run multiple times — only unlocks, never revokes.
    Called once at startup so old databases get their achievements updated automatically.
    """
    from app.models.user import User
    try:
        rows = await session.execute(select(User))
        users = rows.scalars().all()
        for user in users:
            try:
                await check_and_unlock(user.id, session)
            except Exception as exc:
                logger.warning("backfill failed for user %s: %s", user.id, exc)
                try:
                    await session.rollback()
                except Exception:
                    pass
        logger.info("Achievement backfill complete for %d users", len(users))
    except Exception as exc:
        logger.error("backfill_achievements error: %s", exc)
