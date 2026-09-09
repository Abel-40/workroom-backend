"""Profession and skill catalogs, and the professional half of a member's
profile.

Structured like ``api/routers/event_types.py`` for the catalog reads and
``api/routers/members.py`` for the per-member writes: the company is always
re-derived from the authenticated user, and a member id names somebody inside
that company rather than selecting one.
"""

from typing import Literal
from uuid import UUID

from asgiref.sync import sync_to_async
from company.services import resolve_company_context
from ninja import Router, Schema
from pydantic import Field
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate
from workforce import services
from workforce.models import AssignmentPolicy, MemberSkill
from workforce.workload import effective_enforcement_sync, member_workload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['workforce'])
auth = JWTBearerAuth()

# Mirrors MemberSkill.Level. Written out rather than derived so an invalid
# level is a 422 from the schema, before any service code runs.
LevelLiteral = Literal['learning', 'working', 'strong', 'expert']


class SkillIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    category: str = Field(default='', max_length=60)
    description: str = Field(default='', max_length=2_000)


class ProfessionIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2_000)


class MemberSkillIn(Schema):
    skill_id: UUID
    level: LevelLiteral = 'working'


class MemberSkillsIn(Schema):
    # The whole list, not a delta: the caller is editing a list they can see
    # in full, and a merge would leave no way to remove a skill.
    skills: list[MemberSkillIn]


class MemberProfessionIn(Schema):
    profession_id: UUID | None = None


class CapacityIn(Schema):
    weekly_capacity_hours: int | None = Field(default=None, ge=0, le=168)
    availability: dict | None = None


def skill_data(skill) -> dict:
    return {
        'id': str(skill.id),
        'name': skill.name,
        'category': skill.category,
        'description': skill.description,
        'is_default': skill.default_skill_id is not None,
    }


def profession_data(profession) -> dict:
    return {
        'id': str(profession.id),
        'name': profession.name,
        'description': profession.description,
        'is_default': profession.default_profession_id is not None,
    }


async def member_profile_data(profile) -> dict:
    skills = [
        {
            'id': str(row.id),
            'skill_id': str(row.skill_id),
            'name': row.skill.name,
            'category': row.skill.category,
            'level': row.level,
        }
        async for row in MemberSkill.objects.filter(profile=profile).select_related('skill')
    ]
    eligible = await services.member_is_ai_eligible(profile, skill_count=len(skills))
    return {
        'user_id': str(profile.user_id),
        'profession': profession_data(profile.profession_ref) if profile.profession_ref_id else None,
        'skills': skills,
        'weekly_capacity_hours': profile.weekly_capacity_hours,
        'availability': profile.availability or {},
        # Stated positively and with its reason, so the profile reads as an
        # incentive rather than a scolding. §3 asks for a nudge, not a chore.
        'ai_recommendation_eligible': eligible,
        'ai_recommendation_hint': (
            None if eligible else
            'Add a profession or at least one skill to be suggested for task assignments.'
        ),
    }


# --------------------------------------------------------------------------
# Catalogs
# --------------------------------------------------------------------------

@router.get('/skills/', auth=auth, response={200: ApiResponse, 400: ApiResponse})
async def list_skills(request, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
                      category: str | None = None, search: str | None = None):
    context = await resolve_company_context(request.auth)
    if context is None:
        return payload('You must belong to a company.', 400, False)
    queryset = await services.list_skills(context.company, category=category, search=search)
    items, meta = await paginate(queryset, page, page_size)
    return payload('Skills retrieved successfully.', 200, True, {
        'results': [skill_data(skill) for skill in items], 'meta': meta,
    })


@router.post('/skills/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse})
async def create_skill(request, data: SkillIn):
    context = await resolve_company_context(request.auth)
    if context is None:
        return payload('You must belong to a company.', 400, False)
    if context.role not in services.CATALOG_ADMIN_ROLES:
        return payload('Only an owner or company manager may add to the skill catalog.', 403, False)
    skill, error = await services.create_skill(
        context.company, name=data.name, category=data.category, description=data.description,
    )
    if error == 'duplicate_name':
        return payload(
            'A skill with that name already exists.', 400, False,
            errors={'name': ['Already in this catalog']},
        )
    return payload('Skill created successfully.', 201, True, {'skill': skill_data(skill)})


@router.get('/professions/', auth=auth, response={200: ApiResponse, 400: ApiResponse})
async def list_professions(request, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
                           search: str | None = None):
    context = await resolve_company_context(request.auth)
    if context is None:
        return payload('You must belong to a company.', 400, False)
    queryset = await services.list_professions(context.company, search=search)
    items, meta = await paginate(queryset, page, page_size)
    return payload('Professions retrieved successfully.', 200, True, {
        'results': [profession_data(item) for item in items], 'meta': meta,
    })


@router.post('/professions/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse})
async def create_profession(request, data: ProfessionIn):
    context = await resolve_company_context(request.auth)
    if context is None:
        return payload('You must belong to a company.', 400, False)
    if context.role not in services.CATALOG_ADMIN_ROLES:
        return payload('Only an owner or company manager may add to the profession catalog.', 403, False)
    profession, error = await services.create_profession(
        context.company, name=data.name, description=data.description,
    )
    if error == 'duplicate_name':
        return payload(
            'A profession with that name already exists.', 400, False,
            errors={'name': ['Already in this catalog']},
        )
    return payload('Profession created successfully.', 201, True, {'profession': profession_data(profession)})


# --------------------------------------------------------------------------
# One member's professional profile
# --------------------------------------------------------------------------

async def _resolve_target(request, member_user_id, *, for_write):
    """Returns (context, profile, error_response). The member id is looked up
    inside the caller's own company, so a cross-company id is a 404 rather
    than a leak."""
    context = await resolve_company_context(request.auth)
    if context is None:
        return None, None, payload('You must belong to a company.', 400, False)
    profile, error = await services.get_member_profile(request.auth, context.company, member_user_id)
    if error:
        return None, None, payload('Member not found.', 404, False)
    if for_write and not services.can_edit_member_profile(context, profile):
        return None, None, payload('You may only change your own professional profile.', 403, False)
    return context, profile, None


@router.get(
    '/members/{member_user_id}/profile/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 404: ApiResponse},
)
async def get_member_professional_profile(request, member_user_id: UUID):
    _, profile, error_response = await _resolve_target(request, member_user_id, for_write=False)
    if error_response:
        return error_response
    return payload('Profile retrieved successfully.', 200, True, {'profile': await member_profile_data(profile)})


@router.put(
    '/members/{member_user_id}/skills/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def set_member_skills(request, member_user_id: UUID, data: MemberSkillsIn):
    _, profile, error_response = await _resolve_target(request, member_user_id, for_write=True)
    if error_response:
        return error_response
    _, error = await services.set_member_skills(
        profile, [{'skill_id': entry.skill_id, 'level': entry.level} for entry in data.skills],
    )
    if error == 'invalid_skill':
        return payload(
            'One or more skills are not in this company catalog.', 400, False,
            errors={'skills': ['Unknown skill']},
        )
    if error == 'invalid_level':
        return payload('Unknown skill level.', 400, False, errors={'level': ['Invalid level']})
    return payload('Skills updated successfully.', 200, True, {'profile': await member_profile_data(profile)})


@router.patch(
    '/members/{member_user_id}/profession/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def set_member_profession(request, member_user_id: UUID, data: MemberProfessionIn):
    _, profile, error_response = await _resolve_target(request, member_user_id, for_write=True)
    if error_response:
        return error_response
    _, error = await services.set_member_profession(profile, data.profession_id)
    if error == 'invalid_profession':
        return payload(
            'That profession is not in this company catalog.', 400, False,
            errors={'profession_id': ['Unknown profession']},
        )
    return payload('Profession updated successfully.', 200, True, {'profile': await member_profile_data(profile)})


@router.patch(
    '/members/{member_user_id}/capacity/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def set_member_capacity(request, member_user_id: UUID, data: CapacityIn):
    """Weekly hours and availability.

    Capacity is per-company, so it lives on the membership -- somebody working
    three days here and two days elsewhere has two different capacities, and
    one number on the User would be wrong for both.
    """
    _, profile, error_response = await _resolve_target(request, member_user_id, for_write=True)
    if error_response:
        return error_response
    updates = data.model_dump(exclude_unset=True)
    fields = []
    if 'weekly_capacity_hours' in updates:
        profile.weekly_capacity_hours = updates['weekly_capacity_hours']
        fields.append('weekly_capacity_hours')
    if 'availability' in updates:
        normalized, error = services.validate_availability(updates['availability'])
        if error:
            return payload(
                'Availability is not in a shape this API understands.', 400, False,
                errors={error: ['Invalid availability']},
            )
        profile.availability = normalized
        fields.append('availability')
    if fields:
        await profile.asave(update_fields=fields)
    return payload('Capacity updated successfully.', 200, True, {'profile': await member_profile_data(profile)})


# --------------------------------------------------------------------------
# The assignment policy
# --------------------------------------------------------------------------

class AssignmentPolicyIn(Schema):
    enabled: bool | None = None
    max_active_tasks: int | None = Field(default=None, ge=1, le=500)
    max_utilisation_pct: int | None = Field(default=None, ge=1, le=1000)
    enforcement: Literal['warn', 'block'] | None = None
    override_roles: list[Literal['Owner', 'CM', 'DL', 'DM']] | None = None


def policy_data(policy, effective) -> dict:
    if policy is None:
        return {
            'enabled': False, 'max_active_tasks': None, 'max_utilisation_pct': None,
            'enforcement': 'warn', 'override_roles': [], 'effective_enforcement': effective,
        }
    return {
        'enabled': policy.enabled,
        'max_active_tasks': policy.max_active_tasks,
        'max_utilisation_pct': policy.max_utilisation_pct,
        'enforcement': policy.enforcement,
        'override_roles': policy.override_roles or [],
        # What the plan actually permits, which is not always what the row
        # says: a company configured to block but without the entitlement
        # degrades to warn, and one without either feature is off entirely.
        'effective_enforcement': effective,
    }


@router.get('/assignment-policy/', auth=auth, response={200: ApiResponse, 400: ApiResponse})
async def get_assignment_policy(request):
    context = await resolve_company_context(request.auth)
    if context is None:
        return payload('You must belong to a company.', 400, False)
    policy = await AssignmentPolicy.objects.filter(company=context.company).afirst()
    effective = await sync_to_async(effective_enforcement_sync, thread_sensitive=True)(context.company, policy)
    return payload('Policy retrieved successfully.', 200, True, {'policy': policy_data(policy, effective)})


@router.put('/assignment-policy/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse})
async def set_assignment_policy(request, data: AssignmentPolicyIn):
    """Owner or company manager only -- this decides whether other managers
    can be stopped from assigning work, which is a company-structure decision
    rather than a project one."""
    context = await resolve_company_context(request.auth)
    if context is None:
        return payload('You must belong to a company.', 400, False)
    if context.role not in services.CATALOG_ADMIN_ROLES:
        return payload('Only an owner or company manager may change the assignment policy.', 403, False)

    policy, _ = await AssignmentPolicy.objects.aget_or_create(company=context.company)
    updates = data.model_dump(exclude_unset=True)
    for field in ('enabled', 'max_active_tasks', 'max_utilisation_pct', 'enforcement', 'override_roles'):
        if field in updates:
            setattr(policy, field, updates[field])
    await policy.asave()
    effective = await sync_to_async(effective_enforcement_sync, thread_sensitive=True)(context.company, policy)
    return payload('Policy updated successfully.', 200, True, {'policy': policy_data(policy, effective)})


@router.get(
    '/members/{member_user_id}/workload/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 404: ApiResponse},
)
async def get_member_workload(request, member_user_id: UUID):
    """One member's current load.

    Readable by any member of the same company: this is the figure a manager
    consults before assigning, and gating it would make the policy's warning
    the first time anybody sees a number. It carries no per-person performance
    measure -- see §8, which rules those out deliberately.
    """
    context, profile, error_response = await _resolve_target(request, member_user_id, for_write=False)
    if error_response:
        return error_response
    snapshot = await member_workload(context.company, profile.user, profile=profile)
    return payload('Workload retrieved successfully.', 200, True, {'workload': snapshot.as_dict()})
