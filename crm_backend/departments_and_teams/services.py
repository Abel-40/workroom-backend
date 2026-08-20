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
from company.services import get_managed_company, is_company_member
from django.contrib.auth import get_user_model

from .models import Department, Team

User = get_user_model()


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
    return team, None
