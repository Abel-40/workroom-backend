"""Pydantic schemas used by the Django Ninja API.

Django Ninja builds its request parsing, validation, OpenAPI schema, and response
serialization on Pydantic.  Keep HTTP payload types here, separate from Django
ORM models and business logic.
"""

from typing import Any

from ninja import Schema
from pydantic import Field


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
    name: str = Field(min_length=1, max_length=255)
    owner: int = Field(gt=0)
    sector: int = Field(gt=0)


class SelectionIn(Schema):
    company_id: int = Field(gt=0)
    selected_types: list[int] = Field(default_factory=list)
    use_all_default_task_types: bool = False
    use_all_default_departments: bool = False


class InviteIn(Schema):
    email: str = Field(min_length=3, max_length=254)
    department: int | None = Field(default=None, gt=0)
    role: str = Field(default='Owner', max_length=200)


class AcceptInviteIn(Schema):
    token: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    username: str = Field(min_length=1, max_length=100)


class CheckoutIn(Schema):
    plan_id: int = Field(gt=0)
