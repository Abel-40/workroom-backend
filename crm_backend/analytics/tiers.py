"""Who may see which analytics.

Five tiers, widening from "your own numbers" to "everyone's". The rule that
shapes all of them: **a number about a named person is a different thing from
a number about a project**, and the tier boundaries are drawn where names
appear.

    PERSONAL          everyone, about themselves
    PROJECT_AGGREGATE project VIEW -- counts and percentages, no names
    PROJECT_PEOPLE    project MANAGE -- per-member counts, that project only
    DEPARTMENT        DL (own department), CM, Owner
    COMPANY           CM, Owner

Before this, the company-wide workload roster -- every member, their name,
their open task counts -- was readable by every role including a Department
Member. That is one join away from a performance dashboard, which §8 is
explicit about not building.

What is deliberately absent, and must stay absent: any per-person on-time
percentage, velocity, productivity score, or ranking. §8 rules those out "not
now, not later without a separate explicit decision". They are a liability
surface rather than a feature waiting to be asked for -- the numbers look
objective, they are not, and once shipped they end up in performance reviews.
"""

from company.services import get_company_role, get_member_department_id
from projects_and_tasks.access import AccessLevel, resolve_project_access
from users.models import CompanyUserProfile

COMPANY_TIER_ROLES = (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER)
DEPARTMENT_TIER_ROLES = COMPANY_TIER_ROLES + (CompanyUserProfile.Role.DEPARTMENT_LEADER,)


async def can_view_company_analytics(user, company) -> bool:
    """Company-wide figures, including the member roster. CM and Owner only."""
    role = await get_company_role(user, company)
    return role in COMPANY_TIER_ROLES


async def can_view_department_analytics(user, company, department_id=None) -> bool:
    """Department figures. Owner and CM see any department; a Department
    Leader sees their own and no other.

    ``department_id`` of None means "the department breakdown for the whole
    company", which is a company-tier question -- a DL asking it would be
    reading every other department's numbers through a different door.
    """
    role = await get_company_role(user, company)
    if role in COMPANY_TIER_ROLES:
        return True
    if role != CompanyUserProfile.Role.DEPARTMENT_LEADER:
        return False
    if department_id is None:
        return False
    own = await get_member_department_id(user, company)
    return own is not None and own == department_id


async def can_view_project_people(user, project) -> bool:
    """Per-member counts for one project's members.

    Project MANAGE, and scoped to that project's people only. This is the tier
    where names reappear, and it is bounded by the project rather than the
    company on purpose: a project manager needs to know who on *their* project
    is overloaded, which is not the same as being able to enumerate everyone.
    """
    access = await resolve_project_access(user, project)
    return access is not None and access >= AccessLevel.MANAGE


async def can_view_project_aggregate(user, project) -> bool:
    """Counts and percentages for one project. Anyone who can see it.

    Never includes names. A project's completion rate is a fact about the
    work; who is behind on it is a fact about a person.
    """
    return await resolve_project_access(user, project) is not None
