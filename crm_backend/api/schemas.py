"""Pydantic schemas used by the Django Ninja API.

Django Ninja builds its request parsing, validation, OpenAPI schema, and response
serialization on Pydantic.  Keep HTTP payload types here, separate from Django
ORM models and business logic.
"""

from typing import Any, Literal
from uuid import UUID

from ninja import Schema
from pydantic import Field, HttpUrl


class ApiResponse(Schema):
    success: bool
    message: str
    statusCode: int
    data: dict[str, Any] = Field(default_factory=dict)
    errors: dict[str, Any] | None = None


class SignUpIn(Schema):
    email: str = Field(min_length=3, max_length=200)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128)
    first_name: str = Field(default='', max_length=150)
    last_name: str = Field(default='', max_length=150)


class SignInIn(Schema):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=128)


class CompanyRegistrationIn(Schema):
    """``owner`` is intentionally absent: ownership is derived from the
    authenticated request, never accepted from the client (see Rule 3)."""

    name: str = Field(min_length=1, max_length=255)
    sector: UUID


class SelectionIn(Schema):
    company_id: UUID
    selected_types: list[UUID] = Field(default_factory=list)
    use_all_default_task_types: bool = False
    use_all_default_departments: bool = False


class InviteIn(Schema):
    """``role`` excludes 'Owner': a company has exactly one owner, established
    at registration, and invitations must not be able to mint a second one."""

    email: str = Field(min_length=3, max_length=254)
    department: UUID | None = None
    role: Literal['DL', 'DM', 'CM'] = 'DM'


class AcceptInviteIn(Schema):
    token: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    username: str = Field(min_length=1, max_length=100)


class CheckoutIn(Schema):
    plan_id: UUID


class AIAssistantIn(Schema):
    """``reference_url`` uses HttpUrl, which restricts to http/https schemes
    by construction -- scheme allowlisting for the assistant's URL-fetch
    capability is free at this layer (see utils/safe_fetch.py for the rest
    of the SSRF protections)."""

    question: str = Field(min_length=1, max_length=2000)
    reference_url: HttpUrl | None = None


class AIPlanRequestIn(Schema):
    """``mentioned_user_ids`` are @@-mentioned company members the requester
    referenced while describing the work -- passed to the AI as informational
    context only, never a structured assignment instruction (see
    ai_agent/services.py::request_project_plan)."""

    prompt: str = Field(min_length=1, max_length=4000)
    mentioned_user_ids: list[UUID] = Field(default_factory=list)


class AIGeneratedTaskCommentIn(Schema):
    comment: str = Field(min_length=1, max_length=2000)


class AIGeneratedTaskAssignIn(Schema):
    assigned_to_id: UUID | None = None


class AITaskRegenerateIn(Schema):
    instructions: str = Field(default='', max_length=2000)
