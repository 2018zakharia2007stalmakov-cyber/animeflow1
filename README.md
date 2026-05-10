# ANIMEFLOW

Anime streaming web app — FastAPI + SQLite + Jinja2 + Tailwind (CDN) + Plyr.

## Run

```bash
cd artifacts/api-server
PORT=${PORT:-8080} SESSION_SECRET=dev-secret \
  python -m uvicorn main:app --host 0.0.0.0 --port $PORT
```

The DB is auto-created on first start at `data/animeflow.db` and seeded with 5 anime, 3 episodes each.

## Routes

- `/` — каталог аниме
- `/anime/{id}` — страница аниме (player + episodes)
- `/anime/{id}/episode/{n}` — конкретный эпизод
- `/search?q=` — поиск
- `/login`, `/register`, `/logout`, `/profile`
- `/top`, `/schedule`, `/random`, `/admin`
- `/api/healthz`, `/api/search?q=`, `/api/favorites/toggle`, `/api/progress`
