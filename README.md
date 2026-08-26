# Workroom Backend

Workroom is a multi-tenant work-management backend built with Django, PostgreSQL, and **Django Ninja**. Its HTTP API uses **Pydantic schemas** for FastAPI-style, typed request validation, response serialization, and OpenAPI documentation. The active API handlers are asynchronous and use Django's async ORM.

## API

Start the server from the repository root:

```powershell
.\\venv\\Scripts\\uvicorn.exe crm_backend.asgi:application --app-dir crm_backend --reload
```

Remove `--reload` in production and run Uvicorn behind a reverse proxy/process manager.

- Interactive API docs: `http://localhost:8000/api/v1/docs`
- OpenAPI JSON: `http://localhost:8000/api/v1/openapi.json`
- API base path: `http://localhost:8000/api/v1/`
- Health check (unversioned, for infra healthchecks): `http://localhost:8000/api/health/`

Example Pydantic-validated request:

```http
POST /api/v1/auth/signup/
Content-Type: application/json

{
  "email": "owner@example.com",
  "username": "owner",
  "password": "use-a-strong-password"
}
```

Invalid request bodies automatically return a structured `422` response that identifies the invalid field.

## Stack

- Python 3.13 and Django
- Django Ninja + Pydantic v2
- PostgreSQL via Psycopg 3 (pool-capable)
- Uvicorn ASGI server
- SimpleJWT access/refresh tokens
- Stripe Checkout
- Celery + Redis for background jobs (see DEPLOYMENT.md)
- `requirements.txt` (pinned) is the single dependency source of truth

## Development checks

```powershell
.\\venv\\Scripts\\python.exe crm_backend\\manage.py check
.\\venv\\Scripts\\python.exe crm_backend\\manage.py makemigrations --check --dry-run
```

See [PHASE.md](../PHASE.md) and [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md) for the phased scope and current implementation status, and [DEPLOYMENT.md](DEPLOYMENT.md) for running this in Docker and deploying it.

## Async database notes

- API handlers use `async def` and Django async ORM methods such as `afirst()`, `aexists()`, `acreate()`, `asave()`, async iteration, and `abulk_create()`.
- Persistent Django database connections are disabled (`CONN_MAX_AGE = 0`) for async safety. Use a PostgreSQL/Psycopg connection pool in deployment.
- Django does not currently support transaction blocks directly in async code. Multi-write flows therefore run in one small, atomic synchronous helper via `sync_to_async()`; do not set `DJANGO_ALLOW_ASYNC_UNSAFE`.
