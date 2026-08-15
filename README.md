# Workroom Backend

Workroom is a multi-tenant work-management backend built with Django, PostgreSQL, and **Django Ninja**. Its HTTP API uses **Pydantic schemas** for FastAPI-style, typed request validation, response serialization, and OpenAPI documentation.

## API

Start the server from the repository root:

```powershell
.\\venv\\Scripts\\python.exe crm_backend\\manage.py runserver
```

- Interactive API docs: `http://localhost:8000/api/docs`
- OpenAPI JSON: `http://localhost:8000/api/openapi.json`
- API base path: `http://localhost:8000/api/`

Example Pydantic-validated request:

```http
POST /api/auth/signup/
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
- PostgreSQL
- SimpleJWT access/refresh tokens
- Stripe Checkout
- Pipenv dependency management

## Development checks

```powershell
.\\venv\\Scripts\\python.exe crm_backend\\manage.py check
.\\venv\\Scripts\\python.exe crm_backend\\manage.py makemigrations --check --dry-run
```

See [PROJECT_STATUS_AND_PRODUCTION_GAPS.md](PROJECT_STATUS_AND_PRODUCTION_GAPS.md) for the current implementation assessment and production-readiness work.
