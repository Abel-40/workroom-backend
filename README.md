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

## Authorization model

Every company-owned resource is scoped to the authenticated user's own,
server-derived membership -- never to a `company_id`, `owner_id`, or role
supplied by the client. A user who changes an id in a request to reach
another company's data gets `403`/`404`, not the resource.

### Company-level roles

`Owner` (one per company, minted at registration, never a valid demotion/
removal target) → `CM` (Company Manager) → `DL` (Department Leader,
scoped to their own department) → `DM` (Department Member). Resolved by
`company.services.resolve_company_context(user, company_id=None)`, which
returns the caller's membership row alongside their company, not just the
company -- everything from department to capacity to notification
preference lives on that row.

### Project access: VIEW / CONTRIBUTE / MANAGE

A second, project-scoped axis sits underneath the company roles, resolved by
one function everything calls -- `projects_and_tasks.access
.resolve_project_access(user, project)`:

```
VIEW        discovery only -- never implies the ability to change anything.
CONTRIBUTE  work on what you're assigned/added to: move your tasks, submit
            evidence, log time, comment, upload, propose.
MANAGE      shape the work: create/edit/assign tasks, edit the project,
            deadlines, ownership transfer, visibility (within limits).
```

Effective access is the **maximum** of every grant that applies: company
Owner/CM and a project's `current_owner` always get MANAGE; a department
leader gets MANAGE on their own department's projects; an explicit
`ProjectMembership` row names a per-person grant (viewer/contributor/
manager); being the assignee of any live task grants CONTRIBUTE on that
project; a visibility match (company/department/public) grants VIEW and
nothing more. Project *visibility* is a discovery setting, not a permission
-- it was the source of most of this model's original defects, all closed
now (see `docs/PERMISSION_INVENTORY.md`).

A task's fields have two separate owners: the assignee owns description,
checklist, estimate, and moving To Do↔In Progress; MANAGE owns title,
priority, type, department, assignee, deadline, and archive. A request
mixing fields from both sides is refused as a whole, not partially applied.

## Three records of "something happened"

Workroom keeps three, and they answer three different questions -- reach
for the one that matches what's actually being asked, not the one that's
easiest to bolt onto an existing call site:

| | Answers | Audience | Shape |
|---|---|---|---|
| **`notifications_and_activity.CompanyActivity`** | "What's going on here lately?" | Any member | Curated, deliberately incomplete, written to be skimmed |
| **`notifications_and_activity.Notification`** | "What needs *your* attention?" | Per-recipient, permission-aware | Delivered / read / dismissed |
| **`audit.AuditEvent`** | "Who changed this, when, from what, to what, and why?" | Nobody yet (no UI/export) | Append-only, complete for the actions it covers |

`AuditEvent` exists because it cannot be reconstructed retroactively -- the
day someone needs to know who moved a deadline or transferred ownership, the
row was either written at the time or the answer is gone. Every consequential
mutation writes through a single function, `audit.services.record_event()`,
so a real event bus can replace that one indirection later without touching
any call site. Rows are enforced append-only at three layers (model `save`/
`delete`, the queryset, and Django admin) -- application code cannot edit or
remove one, by construction.
