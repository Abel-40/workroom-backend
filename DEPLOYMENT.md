# Deployment (Phase 11)

## Local development (Docker Compose)

```
docker compose up -d
```

Starts Postgres, Redis, the Django app (`crud_bd`, hot-reload via uvicorn),
two Celery workers (`celery_worker_simple`, `celery_worker_heavy` -- see
"Queues" below), and `celery_beat`. `crud_bd`'s startup command runs
`migrate` and `collectstatic` before serving, since this is a single dev
instance -- see "Release procedure" for why that's not true in production.

The sibling `workroom-ai` service isn't part of this compose project (kept
independently deployable). `celery_worker_heavy` reaches it at
`http://host.docker.internal:8001`, so start `workroom-ai`'s own
`docker compose up -d` alongside this one.

## Queues

Two Celery queues, each with its own worker process:

- **`simple`** (`CELERY_TASK_DEFAULT_QUEUE`): fast jobs -- currently invite
  email delivery (`users/tasks.py`) and the invite-retry sweep Celery Beat
  runs every 15 minutes.
- **`heavy`**: slow/external-call work -- currently only
  `ai_agent.tasks.process_ai_generation`, which calls the FastAPI AI
  service and can take up to `WORKROOM_AI_SERVICE_TIMEOUT` seconds. Kept
  separate so a slow AI request never delays invite emails, and vice versa.

Route a new task to `heavy` by adding it to `CELERY_TASK_ROUTES` in
`settings.py`; everything else defaults to `simple`.

## Production (standalone image, no compose)

The Dockerfile's own `CMD` (gunicorn + `uvicorn_worker.UvicornWorker`) is
what runs when this image is deployed without docker-compose, e.g. on a PaaS.
It does **not** run `migrate` or `collectstatic` automatically -- see below.

## Release procedure

Never let the running server auto-migrate on every boot in a real
multi-replica deployment: concurrent replicas starting at once would race on
the same migration. Migrate as an explicit, reviewed, single-instance step
before rolling out new replicas:

```
python manage.py migrate --noinput
python manage.py collectstatic --noinput
```

Then deploy the new image. The docker-compose dev setup auto-runs both on
every boot because it's always a single instance -- that shortcut doesn't
apply here.

## Static & media files

Static files (mainly `/admin/`) are served in-process by WhiteNoise
(`whitenoise.middleware.WhiteNoiseMiddleware`) after `collectstatic` --
neither uvicorn nor gunicorn serve `static/` on their own the way Django's
`runserver` does in `DEBUG`.

Media (user uploads: project images, task attachments) stays on local disk
for V1. That's a known limitation once you run more than one instance or
need durability across redeploys -- move `MEDIA_ROOT`-backed storage to an
object store (S3/GCS-compatible) before scaling beyond a single instance.
This isn't implemented yet; don't half-wire it without real credentials.

## Reverse proxy

`deploy/nginx.conf.example` is a reference config (HTTPS redirect, proxy to
the app on :8000, upload size matching `DATA_UPLOAD_MAX_MEMORY_SIZE`). It's
not run by docker-compose -- there's no real domain or TLS certificate here.
Copy it to your actual host and fill in `server_name`/certificate paths.

## Database backups

`scripts/backup_db.sh` wraps `pg_dump` for a self-managed Postgres instance.
Prefer your host's managed automated-backup product (e.g. RDS snapshots)
when available; this script is the fallback, not a replacement.

## Error tracking

Not pre-wired. When you have a Sentry (or equivalent) DSN, add `sentry-sdk`
and initialize it conditionally on `SENTRY_DSN` being set in `settings.py` --
deliberately not stubbed in now with no DSN to point at (see CLAUDE.md: no
placeholder implementation presented as finished).

## Rate limiting

`utils/rate_limit.py` guards `signup`/`signin`/`send_invite`/`accept_invite`
against brute-force/spam via a Redis fixed-window counter. Set
`RATE_LIMIT_ENABLED=False` as an emergency kill-switch if the limiter itself
ever misbehaves; it already fails open (lets requests through, logs a
warning) if Redis is unreachable, since it's a defense-in-depth control, not
these endpoints' primary security boundary (auth/tenant checks are).

## CI

`.github/workflows/ci.yml` runs ruff and the pytest suite (against real
Postgres/Redis service containers) on every push/PR. `black` is configured
(`pyproject.toml`) but not enforced in CI yet -- the existing codebase was
never actually run through it, so `black --check .` would fail on
pre-existing, unrelated files rather than anything this workflow guards
against. Enforcing it needs a deliberate whole-repo reformat first.
