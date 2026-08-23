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
    of the SSRF protections). ``page_ids`` are Workroom pages (pages app) the
    requester explicitly selected as context."""

    question: str = Field(min_length=1, max_length=2000)
    reference_url: HttpUrl | None = None
    page_ids: list[UUID] = Field(default_factory=list)


class AIPlanRequestIn(Schema):
    """``mentioned_user_ids`` are @@-mentioned company members the requester
    referenced while describing the work -- passed to the AI as informational
    context only, never a structured assignment instruction (see
    ai_agent/services.py::request_project_plan). ``assignee_ids`` is a
    different thing entirely: the human-approved pool of people the plan's
    tasks may be suggested-assigned to (validated eligible before the
    generation is even created). ``max_tasks`` hard-caps how many tasks the
    plan may contain; the AI is never trusted to have honored it on its own
    (see ai_agent/tasks.py::_store_generated_tasks_for_review)."""

    prompt: str = Field(min_length=1, max_length=4000)
    mentioned_user_ids: list[UUID] = Field(default_factory=list)
    assignee_ids: list[UUID] = Field(default_factory=list)
    max_tasks: int = Field(default=10, ge=1, le=50)


class AIGeneratedTaskCommentIn(Schema):
    comment: str = Field(min_length=1, max_length=2000)


class AIGeneratedTaskAssignIn(Schema):
    assigned_to_id: UUID | None = None


class AITaskRegenerateIn(Schema):
    instructions: str = Field(default='', max_length=2000)


class PageFolderCreateIn(Schema):
    name: str = Field(min_length=1, max_length=255)
    color: str = Field(default='amber')


class PageBlockIn(Schema):
    type: Literal['heading', 'paragraph', 'list', 'attachment']
    text: str | None = None
    items: list[str] | None = None
    file_name: str | None = None


class PageCreateIn(Schema):
    title: str = Field(min_length=1, max_length=255)
    blocks: list[PageBlockIn] = Field(default_factory=list)
    project_id: UUID | None = None


class PageUpdateIn(Schema):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    blocks: list[PageBlockIn] | None = None


class AssistantSaveAsPageIn(Schema):
    title: str = Field(min_length=1, max_length=255)
    folder_id: UUID | None = None
    new_folder_name: str | None = Field(default=None, max_length=255)
