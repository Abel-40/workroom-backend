"""Business rules for departments and teams.

Both are lightweight, company-scoped organizational groupings. Unlike
projects there's no per-record visibility model: any company member may
list them (company.services.get_member_company), while creating one
requires the same "managed company" standing already used for sending
invitations -- company owner, or a department leader (see
company.services.get_managed_company). That reuses an existing
authorization primitive rather than introducing a new permission scheme.
"""

from asgiref.sync import sync_to_async
from company.services import get_company_role, get_managed_company, get_member_department_id, is_company_member
from django.contrib.auth import get_user_model
from django.db.models import Q
from notifications_and_activity.services import log_department_created, log_team_created
from users.models import CompanyUserProfile

from .models import DefaultDepartment, Department, Team

User = get_user_model()


async def _can_manage_this_department(user, company, department) -> bool:
    """A Department Leader's 'departments:manage' grant is company-wide at
    the flat-permission level (see permissions/roles_permission.yaml's own
    comment on this), so the "own department only" boundary it promises has
    to be enforced here. Owner/CM are unrestricted; a DL may only manage the
    one department their own CompanyUserProfile currently points at."""
    role = await get_company_role(user, company)
    if role != CompanyUserProfile.Role.DEPARTMENT_LEADER:
        return True
    member_department_id = await get_member_department_id(user, company)
    return member_department_id is not None and member_department_id == department.id


async def _resolve_leader(company, leader_id):
    if leader_id is None:
        return None, None
    leader = await User.objects.filter(id=leader_id).afirst()
    if leader is None or not await is_company_member(leader, company):
        return None, 'invalid_leader'
    return leader, None


async def _resolve_members(company, member_ids):
    """Validates every id belongs to the company before it's trusted --
    never let a client attach an arbitrary user id to a team (Rule 3)."""
    if not member_ids:
        return [], None
    users = [user async for user in User.objects.filter(id__in=member_ids)]
    if len(users) != len(set(member_ids)):
        return None, 'invalid_member'
    for candidate in users:
        if not await is_company_member(candidate, company):
            return None, 'invalid_member'
    return users, None


async def create_department(user, *, name, description='', leader_id=None):
    company = await get_managed_company(user)
    if company is None:
        return None, 'forbidden'
    if await Department.objects.filter(company=company, name__iexact=name).aexists():
        return None, 'duplicate_name'
    leader, error = await _resolve_leader(company, leader_id)
    if error:
        return None, error
    department = await Department.objects.acreate(
        company=company, name=name, description=description, leader=leader,
    )
    await sync_to_async(log_department_created, thread_sensitive=True)(department, user)
    return department, None


async def create_team(user, *, name, description='', leader_id=None, member_ids=None):
    company = await get_managed_company(user)
    if company is None:
        return None, 'forbidden'
    if await Team.objects.filter(company=company, name__iexact=name).aexists():
        return None, 'duplicate_name'
    leader, error = await _resolve_leader(company, leader_id)
    if error:
        return None, error
    members, error = await _resolve_members(company, member_ids)
    if error:
        return None, error
    team = await Team.objects.acreate(company=company, name=name, description=description, leader=leader)
    if members:
        await sync_to_async(team.members.set, thread_sensitive=True)(members)
    await sync_to_async(log_team_created, thread_sensitive=True)(team, user)
    return team, None


async def update_department(user, department, *, name=None, description=None):
    """Rename/redescribe rights: the same "managed company" standing already
    used by create_department. Leadership is handled separately (see
    users.services.set_department_leader/revoke_department_leader) since it
    has authorization side effects this plain field update doesn't need.
    Returns (department, error) where error is 'forbidden', 'duplicate_name',
    or None."""
    company = await get_managed_company(user)
    if company is None or department.company_id != company.id:
        return None, 'forbidden'
    if not await _can_manage_this_department(user, company, department):
        return None, 'forbidden'
    if name is not None and name.lower() != department.name.lower():
        if await Department.objects.filter(company=company, name__iexact=name).exclude(id=department.id).aexists():
            return None, 'duplicate_name'
        department.name = name
    if description is not None:
        department.description = description
    await department.asave()
    return department, None


async def update_team(user, team, *, name=None, description=None, member_ids=None):
    """Same shape as update_department; member_ids, when provided, replaces
    the team's membership entirely (matching create_team's _resolve_members
    validation). Returns (team, error)."""
    company = await get_managed_company(user)
    if company is None or team.company_id != company.id:
        return None, 'forbidden'
    if name is not None and name.lower() != team.name.lower():
        if await Team.objects.filter(company=company, name__iexact=name).exclude(id=team.id).aexists():
            return None, 'duplicate_name'
        team.name = name
    if description is not None:
        team.description = description
    members = None
    if member_ids is not None:
        members, error = await _resolve_members(company, member_ids)
        if error:
            return None, error
    await team.asave()
    if members is not None:
        await sync_to_async(team.members.set, thread_sensitive=True)(members)
    return team, None


# --------------------------------------------------------------------------
# Default-department configuration -- shared by the onboarding wizard
# (api.api.create_departments_from_defaults) and the post-registration
# company-config management endpoints, so the dedupe-by-name logic exists in
# exactly one place.
# --------------------------------------------------------------------------

async def apply_default_departments(company, *, use_all=False, selected_ids=None):
    """Creates company Department rows from DefaultDepartment templates for
    ``company``'s sector (or explicit ``selected_ids``), skipping any whose
    name already exists in this company. Returns the list of newly created
    Department instances (empty if everything was already present)."""
    defaults = DefaultDepartment.objects.filter(
        Q(sector_id=company.sector_id) | Q(sector__isnull=True),
    ) if use_all else DefaultDepartment.objects.filter(id__in=selected_ids or [])
    existing_names = {
        name async for name in Department.objects.filter(company=company).values_list('name', flat=True)
    }
    to_create = [
        Department(name=item.name, description=item.description, company=company, default_department=item)
        async for item in defaults if item.name not in existing_names
    ]
    if to_create:
        await Department.objects.abulk_create(to_create)
    return to_create


async def get_default_departments_with_status(company) -> list[dict]:
    """Every DefaultDepartment available to ``company``'s sector, annotated
    with whether it's already enabled (a Department row with that
    default_department exists) -- the post-registration company-config page
    reads this to show "enabled" vs "add" without duplicating template data."""
    enabled_ids = {
        default_id async for default_id in Department.objects.filter(
            company=company, default_department__isnull=False,
        ).values_list('default_department_id', flat=True)
    }
    return [
        {'id': str(item.id), 'name': item.name, 'description': item.description, 'enabled': item.id in enabled_ids}
        async for item in DefaultDepartment.objects.filter(Q(sector_id=company.sector_id) | Q(sector__isnull=True))
    ]
