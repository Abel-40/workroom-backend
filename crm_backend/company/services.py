"""Authenticated company-context resolution.

The authoritative company for a mutation is always derived from the
authenticated user's server-side state (ownership or profile role), never
from a client-supplied ``company_id``. Centralized here so every endpoint
that needs "which company may this user manage" resolves it the same way.
"""

from users.models import CompanyUserProfile

from company.models import Company


async def get_owned_company(user) -> Company | None:
    """The company this user owns, if any."""
    return await Company.objects.filter(owner=user).afirst()


async def get_managed_company(user) -> Company | None:
    """The company this user may manage: the company they own, or, failing
    that, the company where they hold a department-leader profile."""
    company = await get_owned_company(user)
    if company is not None:
        return company
    profile = await CompanyUserProfile.objects.select_related('company').filter(
        user=user, role=CompanyUserProfile.Role.DEPARTMENT_LEADER,
    ).afirst()
    return profile.company if profile else None


async def get_member_company(user) -> Company | None:
    """The company this user belongs to at all: the company they own, or the
    company of any membership profile they hold, regardless of role. This is
    the baseline check for "may this user act within this company's data,"
    as opposed to :func:`get_managed_company`, which is admin-only actions."""
    company = await get_owned_company(user)
    if company is not None:
        return company
    profile = await CompanyUserProfile.objects.select_related('company').filter(user=user).afirst()
    return profile.company if profile else None


async def is_company_member(user, company: Company) -> bool:
    """Whether ``user`` (owner or any profile role) belongs to ``company``.

    Used to validate an arbitrary target user (an assignee, a collaborator)
    against a company, not just the requesting user.
    """
    if company.owner_id == user.id:
        return True
    return await CompanyUserProfile.objects.filter(user=user, company=company).aexists()


async def get_company_role(user, company: Company) -> str | None:
    """'Owner' if the user owns the company, else their profile role, else None."""
    if company.owner_id == user.id:
        return CompanyUserProfile.Role.Owner
    profile = await CompanyUserProfile.objects.filter(user=user, company=company).afirst()
    return profile.role if profile else None
