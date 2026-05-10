"""Anilibria-first parser + auto-update background loop. Python 3.8 compatible.

Behaviour
---------
* **Primary source: Anilibria** (the new ``anilibria.top`` REST API). Every
  catalogue card is built from an Anilibria release — if a release isn't on
  Anilibria, it doesn't enter the catalogue.
* As soon as a release is fetched we materialise an ``Episode`` row for every
  aired episode reported by Anilibria. Each row stores the direct HLS URLs so
  the front-end can switch episodes instantly without re-hitting the API.
* **Shikimori is used only as enrichment** — to upgrade the description, the
  rating and the poster when Anilibria's data is sparse. A failed Shikimori
  request never blocks creation of the catalogue card or its episodes.
* A background task polls **Anilibria** every 30 minutes for newly aired
  episodes and appends fresh "brick" buttons to existing titles.
"""
import asyncio
import logging
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import delete, desc, select

from app.models.anime import Anime, Episode
from database import SessionLocal

logger = logging.getLogger("animeflow.parser")

# --- Anilibria endpoints (new public API, key-less) ------------------------
ANILIBRIA_HOSTS = ("anilibria.top", "api.anilibria.app")
ANILIBRIA_CDN = "https://anilibria.top"
ANILIBRIA_WEB = "https://anilibria.top"
ANILIBRIA_UA = "AnimeFlow/2.0 (+anilibria sync)"

# --- Shikimori (enrichment only) ------------------------------------------
SHIKIMORI_API = "https://shikimori.one/api"
SHIKIMORI_UA = "AnimeFlow/2.0 (shikimori enrichment)"

AUTO_UPDATE_INTERVAL = 30 * 60  # seconds — every 30 minutes
TOP_LIMIT = 100  # auto-update window: refresh the latest 100 titles for new eps
INITIAL_PARSE_LIMIT: Optional[int] = None  # None == ingest every release Anilibria has
REQUEST_DELAY = 0.5  # seconds between Anilibria requests (avoid throttling)
LOG_BUFFER_SIZE = 800


# --------------------------------------------------------------------------- #
#  Series-group derivation (franchise/chronology key)                         #
# --------------------------------------------------------------------------- #

import re as _re

_SERIES_TRIM_RE = _re.compile(
    r"\s+(?:2nd|3rd|\d+(?:st|nd|rd|th)?|season|s\d+|part|movie|film|"
    r"the\s+movie|ova|ona|special|tv|ii+|iv|vi+|ix|x|\d+)$",
    _re.IGNORECASE,
)
_SERIES_PUNCT_RE = _re.compile(r"[^a-zа-яё0-9\s]+", _re.IGNORECASE)
_SERIES_SPACE_RE = _re.compile(r"\s+")


def _series_group_key(name_main: str, name_english: str) -> str:
    """Derive a stable franchise key from a release's titles.

    Strips season/movie/part suffixes, the part of the title after a colon,
    and punctuation, then collapses whitespace. Two releases that resolve to
    the same key are treated as parts of the same franchise.
    """
    base = (name_english or name_main or "").strip()
    if not base:
        return ""
    s = base.lower()
    s = _re.split(r"[:\-—–|]", s, 1)[0]
    while True:
        s2 = _SERIES_TRIM_RE.sub("", s).strip()
        if s2 == s:
            break
        s = s2
    s = _SERIES_PUNCT_RE.sub(" ", s)
    s = _SERIES_SPACE_RE.sub(" ", s).strip()
    return s


# --------------------------------------------------------------------------- #
#  In-memory parser state                                                     #
# --------------------------------------------------------------------------- #

STATE: Dict[str, Any] = {
    "status": "idle",          # idle | running | done | error | stopped
    "progress": 0,             # 0..100
    "processed": 0,            # number of items processed
    "total": 0,                # total items planned
    "message": "",
    "started_at": None,
    "finished_at": None,
    "auto_update": False,      # whether background loop is alive
    "last_auto_update": None,  # ISO timestamp
    "new_episodes_total": 0,   # cumulative episodes added by auto-update
    "source": "anilibria",     # which catalogue source is in use
}

_log_buffer: Deque[Dict[str, Any]] = deque(maxlen=LOG_BUFFER_SIZE)
_log_seq = 0

_run_lock = asyncio.Lock()
_stop_event = asyncio.Event()
_auto_task: Optional["asyncio.Task[None]"] = None
_auto_started = False
_state_listeners: "set[asyncio.Queue[Dict[str, Any]]]" = set()


# --------------------------------------------------------------------------- #
#  Logging primitives                                                         #
# --------------------------------------------------------------------------- #

def _log(level: str, message: str) -> None:
    global _log_seq
    _log_seq += 1
    entry = {
        "id": _log_seq,
        "level": level,
        "ts": datetime.utcnow().isoformat(timespec="seconds"),
        "message": message,
    }
    _log_buffer.append(entry)
    logger.info("[%s] %s", level, message)
    _broadcast()


def _broadcast() -> None:
    snapshot_data = snapshot()
    dead = []
    for q in _state_listeners:
        try:
            q.put_nowait(snapshot_data)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _state_listeners.discard(q)


def subscribe() -> "asyncio.Queue[Dict[str, Any]]":
    q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=64)
    _state_listeners.add(q)
    try:
        q.put_nowait(snapshot())
    except asyncio.QueueFull:
        pass
    return q


def unsubscribe(q: "asyncio.Queue[Dict[str, Any]]") -> None:
    _state_listeners.discard(q)


def snapshot() -> Dict[str, Any]:
    return {
        **STATE,
        "logs": list(_log_buffer)[-80:],
    }


def logs_since(after_id: int = 0) -> List[Dict[str, Any]]:
    return [e for e in _log_buffer if e["id"] > after_id]


# --------------------------------------------------------------------------- #
#  Anilibria HTTP helpers (PRIMARY SOURCE)                                    #
# --------------------------------------------------------------------------- #

async def _anilibria_get(
    client: httpx.AsyncClient, path: str, params: Optional[Dict[str, Any]] = None
) -> Optional[Any]:
    """GET an Anilibria endpoint, transparently failing over between mirrors."""
    last_status = None
    for host in ANILIBRIA_HOSTS:
        url = "https://{}/api/v1{}".format(host, path)
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            last_status = "net:{}".format(exc.__class__.__name__)
            continue
        if resp.status_code != 200:
            last_status = resp.status_code
            continue
        try:
            return resp.json()
        except ValueError:
            last_status = "json"
            continue
    if last_status is not None:
        _log("WARN", "Anilibria {} → {}".format(path, last_status))
    return None


async def _fetch_release_detail(
    client: httpx.AsyncClient, release_id_or_alias
) -> Optional[Dict[str, Any]]:
    data = await _anilibria_get(
        client, "/anime/releases/{}".format(release_id_or_alias)
    )
    return data if isinstance(data, dict) and data.get("id") else None


async def _fetch_anilibria_top(
    client: httpx.AsyncClient, limit: Optional[int]
) -> List[Dict[str, Any]]:
    """Pull releases from Anilibria's catalogue.

    The catalogue endpoint is paginated (50 per page). When ``limit`` is
    ``None`` we walk every page until the API reports we've run out — this is
    how the admin "массовый парсинг (без лимита)" mode ingests the entire
    Anilibria library. We sleep ``REQUEST_DELAY`` seconds between page
    requests to stay polite.
    """
    out: List[Dict[str, Any]] = []
    page = 1
    while True:
        if limit is not None and len(out) >= limit:
            break
        per_page = 50 if limit is None else min(50, limit - len(out))
        if per_page <= 0:
            break
        data = await _anilibria_get(
            client,
            "/anime/catalog/releases",
            {"page": page, "limit": per_page},
        )
        if not isinstance(data, dict):
            break
        items = data.get("data") or []
        if not items:
            break
        out.extend(items)
        meta = data.get("meta") or {}
        pag = meta.get("pagination") or {}
        total_pages = int(pag.get("total_pages") or page)
        STATE["message"] = "Каталог: страница {} из {} (получено {} тайтлов)".format(
            page, total_pages, len(out)
        )
        _broadcast()
        if page >= total_pages:
            break
        page += 1
        await asyncio.sleep(REQUEST_DELAY)
    return out if limit is None else out[:limit]


async def _fetch_anilibria_latest(
    client: httpx.AsyncClient, limit: int = 50
) -> List[Dict[str, Any]]:
    data = await _anilibria_get(
        client, "/anime/releases/latest", {"limit": limit}
    )
    if isinstance(data, list):
        return data
    return []


async def _anilibria_search(
    client: httpx.AsyncClient, query: str
) -> Optional[Dict[str, Any]]:
    if not query:
        return None
    data = await _anilibria_get(
        client, "/app/search/releases", {"query": query, "limit": 1}
    )
    if isinstance(data, list) and data:
        return data[0]
    return None


def _release_titles(rec: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    name = rec.get("name") or {}
    ru = (name.get("main") or "").strip()
    en = (name.get("english") or "").strip()
    alt = (name.get("alternative") or "").strip()
    title = ru or en or "Без названия"
    extras: List[str] = []
    if en and en != title:
        extras.append(en)
    if alt and alt not in (title, en):
        extras.append(alt)
    return title, en, extras


def _absolute(url: Optional[str]) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return ANILIBRIA_CDN + url
    return url


def _release_poster(rec: Dict[str, Any]) -> Tuple[str, str]:
    poster_block = rec.get("poster") or {}
    src = poster_block.get("src") or poster_block.get("preview") or ""
    optimized = poster_block.get("optimized") or {}
    backdrop = optimized.get("src") or optimized.get("preview") or src
    return _absolute(src), _absolute(backdrop)


def _release_genres(rec: Dict[str, Any]) -> str:
    genres = rec.get("genres") or []
    out: List[str] = []
    for g in genres:
        if isinstance(g, dict):
            name = g.get("name")
            if name:
                out.append(str(name))
    return ", ".join(out)


def _release_year(rec: Dict[str, Any]) -> int:
    try:
        return int(rec.get("year") or 0)
    except (TypeError, ValueError):
        return 0


def _release_status(rec: Dict[str, Any]) -> str:
    if rec.get("is_in_production") or rec.get("is_ongoing"):
        return "ongoing"
    return "released"


def _release_episodes(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    episodes = rec.get("episodes") or []
    out: List[Dict[str, Any]] = []
    if not isinstance(episodes, list):
        return out
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        try:
            number = int(ep.get("ordinal") or 0)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        out.append({
            "number": number,
            "name": (ep.get("name") or "").strip(),
            "hls_sd": ep.get("hls_480") or "",
            "hls_hd": ep.get("hls_720") or "",
            "hls_fhd": ep.get("hls_1080") or "",
        })
    out.sort(key=lambda e: e["number"])
    return out


def _build_iframe_url(alias: str, episode_number: int) -> str:
    if not alias:
        return ""
    return "{}/anime/releases/release/{}/episodes".format(ANILIBRIA_WEB, alias)


def _best_video_url(ep_payload: Dict[str, Any], alias: str) -> str:
    for key in ("hls_hd", "hls_fhd", "hls_sd"):
        url = ep_payload.get(key) or ""
        if url:
            return url
    return _build_iframe_url(alias, ep_payload["number"])


# --------------------------------------------------------------------------- #
#  Shikimori enrichment (SECONDARY — never blocks catalogue creation)         #
# --------------------------------------------------------------------------- #

async def _shikimori_enrich(
    client: httpx.AsyncClient, title_ru: str, title_en: str
) -> Dict[str, Any]:
    query = title_en or title_ru
    if not query:
        return {}
    try:
        resp = await client.get(
            SHIKIMORI_API + "/animes",
            params={"search": query, "limit": 1},
            headers={"User-Agent": SHIKIMORI_UA, "Accept": "application/json"},
            timeout=httpx.Timeout(8.0, connect=5.0),
        )
    except httpx.HTTPError:
        return {}
    if resp.status_code != 200:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {}
    if not isinstance(data, list) or not data:
        return {}
    rec = data[0]

    detail: Optional[Dict[str, Any]] = None
    aid = rec.get("id")
    if aid:
        try:
            d = await client.get(
                "{}/animes/{}".format(SHIKIMORI_API, aid),
                headers={"User-Agent": SHIKIMORI_UA, "Accept": "application/json"},
                timeout=httpx.Timeout(8.0, connect=5.0),
            )
            if d.status_code == 200:
                detail = d.json()
        except (httpx.HTTPError, ValueError):
            detail = None

    image = rec.get("image") or {}
    poster = image.get("original") or image.get("preview") or ""
    if poster and poster.startswith("/"):
        poster = "https://shikimori.one" + poster

    out: Dict[str, Any] = {}
    if poster:
        out["poster_url_alt"] = poster
    score = rec.get("score")
    try:
        score_val = float(score or 0.0)
    except (TypeError, ValueError):
        score_val = 0.0
    if score_val:
        out["rating"] = score_val
    if detail:
        desc = (detail.get("description") or "").strip()
        if desc:
            out["description"] = desc[:4000]
        genres = detail.get("genres") or []
        gs = ", ".join(
            (g.get("russian") or g.get("name") or "")
            for g in genres if isinstance(g, dict)
        )
        if gs:
            out["genres_alt"] = gs
    return out


# --------------------------------------------------------------------------- #
#  Episode persistence                                                        #
# --------------------------------------------------------------------------- #

async def _ensure_episodes(
    session,
    anime: Anime,
    episodes_payload: List[Dict[str, Any]],
    alias: str,
) -> int:
    """Sync ``Episode`` rows for ``anime`` from an Anilibria payload."""
    if not episodes_payload:
        return 0

    existing = await session.execute(
        select(Episode).where(Episode.anime_id == anime.id)
    )
    by_number: Dict[int, Episode] = {
        e.episode_number: e for e in existing.scalars().all()
    }

    added = 0
    for ep in episodes_payload:
        number = ep["number"]
        hls_hd = ep.get("hls_hd") or ""
        hls_sd = ep.get("hls_sd") or ""
        hls_fhd = ep.get("hls_fhd") or ""
        iframe = _build_iframe_url(alias, number)
        video_url = _best_video_url(ep, alias)
        title = ep.get("name") or "Эпизод {}".format(number)

        row = by_number.get(number)
        if row is None:
            session.add(
                Episode(
                    anime_id=anime.id,
                    episode_number=number,
                    title=title,
                    video_url=video_url,
                    anilibria_id=anime.anilibria_id,
                    anilibria_host="",
                    anilibria_hls_hd=hls_hd,
                    anilibria_hls_sd=hls_sd,
                    anilibria_hls_fhd=hls_fhd,
                    anilibria_iframe=iframe,
                )
            )
            added += 1
        else:
            row.video_url = video_url or row.video_url
            row.title = title or row.title
            row.anilibria_id = anime.anilibria_id
            row.anilibria_hls_hd = hls_hd or row.anilibria_hls_hd
            row.anilibria_hls_sd = hls_sd or row.anilibria_hls_sd
            row.anilibria_hls_fhd = hls_fhd or row.anilibria_hls_fhd
            row.anilibria_iframe = iframe or row.anilibria_iframe
    return added


# --------------------------------------------------------------------------- #
#  Single-anime full re-parse                                                 #
# --------------------------------------------------------------------------- #

async def reparse_anime(anime_id: int) -> Dict[str, Any]:
    """Wipe and re-import all episodes for one anime from Anilibria.

    Returns a structured report; also pushes log lines into the live admin
    log buffer so progress is visible in the admin panel.
    """
    report: Dict[str, Any] = {
        "anime_id": anime_id,
        "ok": False,
        "deleted": 0,
        "added": 0,
        "total": 0,
        "gaps": [],
        "duplicates": [],
        "title": None,
        "error": None,
    }

    async with SessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            report["error"] = "Аниме не найдено"
            _log("ERROR", "Re-parse: id={} не найдено".format(anime_id))
            return report
        report["title"] = anime.title

    _log("INFO", "Re-parse «{}» (id={}) — начинаю".format(report["title"], anime_id))

    rec: Optional[Dict[str, Any]] = None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": ANILIBRIA_UA, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            if anime.anilibria_id:
                rec = await _fetch_release_detail(client, anime.anilibria_id)
            if rec is None and anime.anilibria_code:
                rec = await _fetch_release_detail(client, anime.anilibria_code)
            if rec is None:
                rec = await _anilibria_search(client, anime.title)
                if rec and rec.get("id"):
                    rec = await _fetch_release_detail(client, rec["id"]) or rec
    except Exception as exc:  # noqa: BLE001
        report["error"] = "Ошибка сети: {}".format(exc)
        _log("ERROR", "Re-parse «{}»: ошибка сети — {}".format(report["title"], exc))
        return report

    if not rec:
        report["error"] = "Релиз не найден на Anilibria"
        _log("WARN", "Re-parse «{}»: релиз не найден на Anilibria".format(report["title"]))
        return report

    episodes_payload = _release_episodes(rec)
    if not episodes_payload:
        report["error"] = "У релиза нет серий на Anilibria"
        _log("WARN", "Re-parse «{}»: серий нет на Anilibria".format(report["title"]))
        return report

    alias = (rec.get("alias") or anime.anilibria_code or "").strip()

    # Detect duplicates and gaps in the upstream list.
    numbers = [int(ep["number"]) for ep in episodes_payload if ep.get("number")]
    seen: set = set()
    for n in numbers:
        if n in seen:
            report["duplicates"].append(n)
        seen.add(n)
    if numbers:
        lo, hi = min(numbers), max(numbers)
        present = set(numbers)
        report["gaps"] = [n for n in range(lo, hi + 1) if n not in present]

    # De-duplicate the payload (keep the first occurrence of each number).
    seen2: set = set()
    clean_payload: List[Dict[str, Any]] = []
    for ep in episodes_payload:
        n = int(ep.get("number") or 0)
        if not n or n in seen2:
            continue
        seen2.add(n)
        clean_payload.append(ep)
    clean_payload.sort(key=lambda e: int(e["number"]))

    async with SessionLocal() as session:
        fresh = await session.get(Anime, anime_id)
        if not fresh:
            report["error"] = "Аниме исчезло во время re-parse"
            return report

        # Wipe existing episodes — clean slate.
        del_res = await session.execute(
            delete(Episode).where(Episode.anime_id == anime_id)
        )
        report["deleted"] = int(del_res.rowcount or 0)

        if not fresh.anilibria_id and rec.get("id"):
            try:
                fresh.anilibria_id = int(rec["id"])
            except (TypeError, ValueError):
                pass
        if alias and not fresh.anilibria_code:
            fresh.anilibria_code = alias

        # Re-create everything in episode-number order so the DB matches Anilibria.
        for ep in clean_payload:
            number = int(ep["number"])
            session.add(
                Episode(
                    anime_id=anime_id,
                    episode_number=number,
                    title=ep.get("name") or "Эпизод {}".format(number),
                    video_url=_best_video_url(ep, alias),
                    anilibria_id=fresh.anilibria_id,
                    anilibria_host="",
                    anilibria_hls_hd=ep.get("hls_hd") or "",
                    anilibria_hls_sd=ep.get("hls_sd") or "",
                    anilibria_hls_fhd=ep.get("hls_fhd") or "",
                    anilibria_iframe=_build_iframe_url(alias, number),
                )
            )

        # Update episodes_total on the anime if the column is present.
        try:
            fresh.episodes_total = max((int(ep["number"]) for ep in clean_payload), default=0)
        except Exception:  # noqa: BLE001
            pass

        await session.commit()

    report["added"] = len(clean_payload)
    report["total"] = report["added"]
    report["ok"] = True

    _log(
        "INFO",
        "Re-parse «{}» готово: удалено {}, добавлено {} (1..{})".format(
            report["title"], report["deleted"], report["added"],
            max(numbers) if numbers else 0,
        ),
    )
    if report["gaps"]:
        _log(
            "WARN",
            "Re-parse «{}»: пропуски в нумерации Anilibria — {}".format(
                report["title"], ", ".join(str(g) for g in report["gaps"][:20])
            ),
        )
    if report["duplicates"]:
        _log(
            "WARN",
            "Re-parse «{}»: дубли номеров от Anilibria (отфильтрованы) — {}".format(
                report["title"], ", ".join(str(g) for g in report["duplicates"][:20])
            ),
        )
    _broadcast()
    return report


# --------------------------------------------------------------------------- #
#  Public control API                                                         #
# --------------------------------------------------------------------------- #

async def run_once() -> bool:
    """Kick off an Anilibria-first catalogue import. Returns False if running."""
    if _run_lock.locked():
        return False

    async def _runner() -> None:
        async with _run_lock:
            _stop_event.clear()
            STATE.update(
                status="running",
                progress=0,
                processed=0,
                total=0,
                message="Подключение к Anilibria…",
                started_at=datetime.utcnow().isoformat(timespec="seconds"),
                finished_at=None,
            )
            _log("INFO", "Старт парсера: ТОП-{} с Anilibria".format(TOP_LIMIT))
            _broadcast()

            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(20.0, connect=10.0),
                    headers={"User-Agent": ANILIBRIA_UA, "Accept": "application/json"},
                    follow_redirects=True,
                ) as client:
                    catalog = await _fetch_anilibria_top(client, INITIAL_PARSE_LIMIT)
                    if not catalog:
                        _log("ERROR", "Не удалось получить релизы с Anilibria")
                        STATE.update(
                            status="error",
                            message="Не удалось получить релизы с Anilibria",
                        )
                        return

                    # Dedup by Anilibria id so a release that appears on
                    # multiple pages (rare during pagination drift) isn't
                    # processed twice in the same run.
                    seen_ids: set = set()
                    unique_catalog: List[Dict[str, Any]] = []
                    for it in catalog:
                        rid = it.get("id")
                        if rid in seen_ids:
                            continue
                        seen_ids.add(rid)
                        unique_catalog.append(it)
                    catalog = unique_catalog

                    STATE["total"] = len(catalog)
                    _log(
                        "INFO",
                        "Получено {} уникальных релизов с Anilibria (массовый парсинг)".format(
                            len(catalog)
                        ),
                    )
                    _broadcast()

                    for idx, summary_rec in enumerate(catalog, start=1):
                        if _stop_event.is_set():
                            _log("WARN", "Парсер остановлен оператором")
                            STATE.update(
                                status="stopped", message="Остановлено оператором"
                            )
                            return

                        title_ru, title_en, alt_bits = _release_titles(summary_rec)
                        STATE.update(
                            processed=idx - 1,
                            progress=int(((idx - 1) / max(1, len(catalog))) * 100),
                            message="[{}/{}] {}".format(idx, len(catalog), title_ru),
                        )
                        _broadcast()

                        # Catalogue payload doesn't include episodes — fetch detail.
                        rid = summary_rec.get("id")
                        detail = await _fetch_release_detail(client, rid) if rid else None
                        rec = detail or summary_rec

                        # Re-derive titles from the detail response (richer).
                        title_ru, title_en, alt_bits = _release_titles(rec)
                        poster, backdrop = _release_poster(rec)
                        episodes_payload = _release_episodes(rec)

                        kwargs = {
                            "title": title_ru,
                            "alternative_titles": ", ".join(
                                dict.fromkeys(a for a in alt_bits if a)
                            ),
                            "description": (rec.get("description") or "").strip()[:4000],
                            "poster_url": poster,
                            "backdrop_url": backdrop,
                            "genres": _release_genres(rec),
                            "year": _release_year(rec),
                            "status": _release_status(rec),
                            "rating": 0.0,
                            "anilibria_id": int(rec.get("id") or 0) or None,
                            "anilibria_code": (rec.get("alias") or "").strip(),
                            "series_group": _series_group_key(title_ru, title_en),
                        }

                        try:
                            extra = await _shikimori_enrich(
                                client, title_ru, title_en
                            )
                        except Exception:
                            extra = {}
                        if extra:
                            if extra.get("description") and len(extra["description"]) > len(kwargs["description"]):
                                kwargs["description"] = extra["description"]
                            if extra.get("rating"):
                                kwargs["rating"] = extra["rating"]
                            if extra.get("poster_url_alt") and not kwargs["poster_url"]:
                                kwargs["poster_url"] = extra["poster_url_alt"]
                            if extra.get("genres_alt") and not kwargs["genres"]:
                                kwargs["genres"] = extra["genres_alt"]

                        added_eps = await _persist_anime(
                            kwargs,
                            episodes_payload=episodes_payload,
                            alias=kwargs["anilibria_code"],
                        )

                        ep_numbers = [e["number"] for e in episodes_payload]
                        ep_min = min(ep_numbers) if ep_numbers else 0
                        ep_max = max(ep_numbers) if ep_numbers else 0
                        _log(
                            "INFO",
                            "[OK] {}: загружено {} серий ({}-{}), +{} новых".format(
                                title_ru, len(episodes_payload), ep_min, ep_max, added_eps
                            ),
                        )

                        STATE.update(
                            processed=idx,
                            progress=int((idx / max(1, len(catalog))) * 100),
                        )
                        _broadcast()
                        await asyncio.sleep(REQUEST_DELAY)

                STATE.update(
                    status="done",
                    progress=100,
                    message="Загрузка завершена: {}/{}".format(
                        STATE["processed"], STATE["total"]
                    ),
                )
                _log(
                    "INFO",
                    "Готово: импортировано {} аниме с Anilibria".format(
                        STATE["processed"]
                    ),
                )
                ensure_auto_update_started()

            except Exception as exc:  # pragma: no cover
                _log("ERROR", "Сбой парсера: {}".format(exc))
                STATE.update(status="error", message=str(exc))
            finally:
                STATE["finished_at"] = datetime.utcnow().isoformat(timespec="seconds")
                _broadcast()

    asyncio.create_task(_runner())
    return True


async def stop() -> bool:
    if not _run_lock.locked():
        return False
    _stop_event.set()
    _log("WARN", "Получен сигнал остановки парсера")
    return True


async def shutdown() -> None:
    global _auto_task, _auto_started
    _stop_event.set()
    if _auto_task and not _auto_task.done():
        _auto_task.cancel()
        try:
            await _auto_task
        except (asyncio.CancelledError, Exception):
            pass
    _auto_task = None
    _auto_started = False
    STATE["auto_update"] = False


# --------------------------------------------------------------------------- #
#  Persistence                                                                #
# --------------------------------------------------------------------------- #

async def _persist_anime(
    kwargs: Dict[str, Any],
    episodes_payload: List[Dict[str, Any]],
    alias: str,
) -> int:
    async with SessionLocal() as session:
        anime: Optional[Anime] = None
        anilibria_id = kwargs.get("anilibria_id")
        if anilibria_id:
            row = await session.execute(
                select(Anime).where(Anime.anilibria_id == anilibria_id)
            )
            anime = row.scalar_one_or_none()
        if anime is None:
            row = await session.execute(
                select(Anime).where(Anime.title == kwargs["title"])
            )
            anime = row.scalar_one_or_none()

        if anime is None:
            anime = Anime(**kwargs)
            session.add(anime)
            await session.flush()
        else:
            for k, v in kwargs.items():
                if v not in (None, ""):
                    setattr(anime, k, v)

        added_eps = await _ensure_episodes(
            session, anime, episodes_payload=episodes_payload, alias=alias
        )
        await session.commit()
        return added_eps


# --------------------------------------------------------------------------- #
#  Auto-update background loop                                                #
# --------------------------------------------------------------------------- #

def ensure_auto_update_started() -> None:
    global _auto_task, _auto_started
    if _auto_started and _auto_task and not _auto_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if not loop.is_running():
        return
    _auto_task = loop.create_task(_auto_update_loop())
    _auto_started = True
    STATE["auto_update"] = True
    _log("INFO", "Авто-обновление с Anilibria включено (каждые 30 минут)")


async def _auto_update_loop() -> None:
    while True:
        try:
            await asyncio.sleep(AUTO_UPDATE_INTERVAL)
            await _auto_update_round()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover
            _log("WARN", "auto-update round failed: {}".format(exc))


async def _auto_update_round() -> None:
    """Poll Anilibria for new episodes and append fresh bricks where needed."""
    if _run_lock.locked():
        return

    async with SessionLocal() as session:
        rows = await session.execute(
            select(Anime).order_by(desc(Anime.id)).limit(TOP_LIMIT)
        )
        anime_list = rows.scalars().all()

    if not anime_list:
        return

    _log("INFO", "Авто-проверка Anilibria: {} тайтлов".format(len(anime_list)))
    new_total = 0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": ANILIBRIA_UA, "Accept": "application/json"},
        follow_redirects=True,
    ) as client:
        for anime in anime_list:
            rec: Optional[Dict[str, Any]] = None
            if anime.anilibria_id:
                rec = await _fetch_release_detail(client, anime.anilibria_id)
            if rec is None and anime.anilibria_code:
                rec = await _fetch_release_detail(client, anime.anilibria_code)
            if rec is None:
                rec = await _anilibria_search(client, anime.title)
                if rec and rec.get("id"):
                    rec = await _fetch_release_detail(client, rec["id"]) or rec
            if not rec:
                continue

            episodes_payload = _release_episodes(rec)
            if not episodes_payload:
                continue
            alias = (rec.get("alias") or anime.anilibria_code or "").strip()

            async with SessionLocal() as s2:
                fresh = await s2.get(Anime, anime.id)
                if not fresh:
                    continue
                if not fresh.anilibria_id and rec.get("id"):
                    try:
                        fresh.anilibria_id = int(rec["id"])
                    except (TypeError, ValueError):
                        pass
                if alias and not fresh.anilibria_code:
                    fresh.anilibria_code = alias

                added = await _ensure_episodes(
                    s2, fresh, episodes_payload=episodes_payload, alias=alias
                )
                if added:
                    await s2.commit()
                    new_total += added
                    _log(
                        "INFO",
                        "Новые серии для «{}»: +{} (всего {})".format(
                            fresh.title, added, len(episodes_payload)
                        ),
                    )
                else:
                    await s2.commit()
            await asyncio.sleep(0.15)

    STATE["last_auto_update"] = datetime.utcnow().isoformat(timespec="seconds")
    STATE["new_episodes_total"] = STATE.get("new_episodes_total", 0) + new_total
    if new_total:
        _log(
            "INFO",
            "Авто-проверка завершена: добавлено {} новых серий".format(new_total),
        )
    else:
        _log("INFO", "Авто-проверка завершена: новых серий не найдено")
    _broadcast()
