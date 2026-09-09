"""Catalog management, capacity, and the eligibility signal.

Conventions follow ``event_management/services.py`` and
``projects_and_tasks/services.py``: every mutation re-derives the company from
authenticated server-side state, references are validated against the caller's
own company before they are trusted, and expected business-rule failures come
back as ``(result, error)`` rather than as exceptions.

Workload arithmetic and the assignment policy live in
:mod:`workforce.workload` -- this module is the people catalog.
"""

from datetime import date

from asgiref.sync import sync_to_async
from django.db.models import Q
from django.db.models.functions import Lower
from users.models import CompanyUserProfile

from .models import DefaultProfession, DefaultSkill, MemberSkill, Profession, Skill

WEEKDAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
DEFAULT_WORKING_DAYS = ('mon', 'tue', 'wed', 'thu', 'fri')

# Roles that may edit somebody else's professional profile. Everyone may edit
# their own -- a skill claim is a statement about yourself, and needing a
# manager's help to record one is how the data stops being kept up to date.
CATALOG_ADMIN_ROLES = (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER)


# --------------------------------------------------------------------------
# Catalog seeding -- mirrors apply_default_event_types / apply_default_task_types
# --------------------------------------------------------------------------

def _templates_for(model, company):
    return model.objects.filter(Q(sector_id=company.sector_id) | Q(sector__isnull=True))


async def apply_default_professions(company, *, use_all=False, selected_ids=None):
    templates = (
        _templates_for(DefaultProfession, company) if use_all
        else DefaultProfession.objects.filter(id__in=selected_ids or [])
    )
    existing = {
        name.lower() async for name in Profession.objects.filter(company=company).values_list('name', flat=True)
    }
    to_create = [
        Profession(name=item.name, description=item.description, company=company, default_profession=item)
        async for item in templates if item.name.lower() not in existing
    ]
    if to_create:
        await Profession.objects.abulk_create(to_create)
    return to_create


async def apply_default_skills(company, *, use_all=False, selected_ids=None):
    templates = (
        _templates_for(DefaultSkill, company) if use_all
        else DefaultSkill.objects.filter(id__in=selected_ids or [])
    )
    existing = {
        name.lower() async for name in Skill.objects.filter(company=company).values_list('name', flat=True)
    }
    to_create = [
        Skill(name=item.name, category=item.category, description=item.description,
              company=company, default_skill=item)
        async for item in templates if item.name.lower() not in existing
    ]
    if to_create:
        await Skill.objects.abulk_create(to_create)
    return to_create


# --------------------------------------------------------------------------
# Custom catalog entries
# --------------------------------------------------------------------------

async def normalize_category(company, category: str) -> str:
    """Reuse an existing category's exact spelling where one matches.

    Categories are display grouping, so they are a plain string -- but two
    spellings of one category split a list in the UI for no reason a user can
    see. Matching case-insensitively against what the company already has
    costs one query and removes the whole class of it.
    """
    candidate = (category or '').strip()
    if not candidate:
        return ''
    match = await Skill.objects.filter(
        company=company, category__iexact=candidate,
    ).values_list('category', flat=True).afirst()
    return match or candidate


async def create_skill(company, *, name, category='', description=''):
    """Returns (skill, error) where error is 'duplicate_name' or None."""
    name = (name or '').strip()
    if await Skill.objects.filter(company=company, name__iexact=name).aexists():
        return None, 'duplicate_name'
    skill = await Skill.objects.acreate(
        company=company, name=name, category=await normalize_category(company, category),
        description=description,
    )
    return skill, None


async def create_profession(company, *, name, description=''):
    """Returns (profession, error) where error is 'duplicate_name' or None."""
    name = (name or '').strip()
    if await Profession.objects.filter(company=company, name__iexact=name).aexists():
        return None, 'duplicate_name'
    profession = await Profession.objects.acreate(company=company, name=name, description=description)
    return profession, None


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

def validate_availability(value) -> tuple[dict | None, str | None]:
    """Check the shape of an ``availability`` payload.

    A ``JSONField`` accepts anything, so the contract only exists where it is
    checked. Returns (normalized, error). Unknown keys are dropped rather than
    rejected: storing a key nothing reads is how a validated field quietly
    stops being one.
    """
    if value in (None, {}):
        return {}, None
    if not isinstance(value, dict):
        return None, 'invalid_availability'

    normalized = {}
    if 'working_days' in value:
        days = value.get('working_days')
        if not isinstance(days, list):
            return None, 'invalid_working_days'
        seen = []
        for day in days:
            token = str(day).strip()[:3].lower()
            if token not in WEEKDAYS:
                return None, 'invalid_working_days'
            if token not in seen:
                seen.append(token)
        if not seen:
            # An empty working week means zero capacity, which is a thing
            # somebody might mean -- but far more often it is a UI sending an
            # empty array, and the two are indistinguishable here. Omitting
            # the key is how you say "the default week".
            return None, 'invalid_working_days'
        normalized['working_days'] = [day for day in WEEKDAYS if day in seen]

    if 'time_off' in value:
        periods = value.get('time_off')
        if not isinstance(periods, list):
            return None, 'invalid_time_off'
        cleaned = []
        for period in periods:
            if not isinstance(period, dict):
                return None, 'invalid_time_off'
            start, end = str(period.get('start', '')), str(period.get('end', ''))
            if not _is_iso_date(start) or not _is_iso_date(end) or end < start:
                return None, 'invalid_time_off'
            cleaned.append({'start': start, 'end': end})
        normalized['time_off'] = cleaned

    return normalized, None


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    return True


# --------------------------------------------------------------------------
# The professional profile
# --------------------------------------------------------------------------

def is_eligible_for_ai_recommendation(profile, *, skill_count=None) -> bool:
    """Whether this member can be suggested as an assignee by the AI.

    A profession or at least one skill. §3 asks for a nudge rather than a
    compliance chore, and this is the nudge: nobody is stopped from doing
    anything, they are simply not recommended for work nobody has recorded
    them as able to do. The profile says so in as many words, so it reads as
    an incentive rather than a punishment.

    Note it does **not** consult the deprecated free-text ``profession``
    string: 'Not provided' was its default, so every member in the database
    would qualify.
    """
    if profile.profession_ref_id is not None:
        return True
    if skill_count is not None:
        return skill_count > 0
    return MemberSkill.objects.filter(profile=profile).exists()


async def member_is_ai_eligible(profile, *, skill_count=None) -> bool:
    """Async wrapper over :func:`is_eligible_for_ai_recommendation`."""
    return await sync_to_async(is_eligible_for_ai_recommendation, thread_sensitive=True)(
        profile, skill_count=skill_count,
    )


async def get_member_profile(user, company, member_user_id):
    """Returns (profile, error) with error 'not_found' or None. Scoped to the
    company the caller actually belongs to -- the id names a member, it never
    selects a company."""
    profile = await CompanyUserProfile.objects.select_related(
        'user', 'department', 'profession_ref',
    ).filter(company=company, user_id=member_user_id).afirst()
    if profile is None:
        return None, 'not_found'
    return profile, None


def can_edit_member_profile(context, target_profile) -> bool:
    """Yourself always; Owner/CM for anybody else.

    The target is only ever looked up inside ``context.company`` (see
    :func:`get_member_profile`), so this does not re-check tenancy -- a
    profile that reached here already belongs to the caller's company.
    """
    if context.membership is not None and context.membership.id == target_profile.id:
        return True
    return context.role in CATALOG_ADMIN_ROLES


async def set_member_profession(profile, profession_id):
    """Set or clear a member's profession. Returns (profile, error).

    Dual-writes the deprecated free-text column for one release so a rollback
    still shows what somebody had chosen; see
    ``CompanyUserProfile.profession``.
    """
    if profession_id is None:
        profile.profession_ref = None
        profile.profession = 'Not provided'
    else:
        profession = await Profession.objects.filter(
            id=profession_id, company_id=profile.company_id,
        ).afirst()
        if profession is None:
            return None, 'invalid_profession'
        profile.profession_ref = profession
        profile.profession = profession.name[:100]
    await profile.asave(update_fields=['profession_ref', 'profession'])
    return profile, None


async def set_member_skills(profile, entries):
    """Replace this member's skill claims with ``entries``.

    ``entries`` is a list of ``{'skill_id': ..., 'level': ...}``. Every skill
    is validated against the member's *own company* catalog before anything is
    written -- a client-supplied skill id from another company must not become
    a row here (Rule 3). Returns (member_skills, error).

    Replace rather than merge, because the caller is editing a list they can
    see in full; a merge would make removing a skill impossible through this
    endpoint.
    """
    wanted = {}
    for entry in entries:
        skill_id = entry.get('skill_id')
        level = entry.get('level') or MemberSkill.Level.WORKING
        if level not in MemberSkill.Level.values:
            return None, 'invalid_level'
        wanted[skill_id] = level

    if wanted:
        valid_ids = {
            skill_id async for skill_id in Skill.objects.filter(
                id__in=list(wanted), company_id=profile.company_id,
            ).values_list('id', flat=True)
        }
        if valid_ids != set(wanted):
            return None, 'invalid_skill'

    await sync_to_async(_replace_member_skills_sync, thread_sensitive=True)(profile, wanted)
    rows = [
        row async for row in MemberSkill.objects.filter(profile=profile).select_related('skill')
    ]
    return rows, None


def _replace_member_skills_sync(profile, wanted: dict):
    """Reconcile in one transaction. Existing rows are updated in place rather
    than deleted and recreated, so ``created_at`` keeps meaning "since when"."""
    from django.db import transaction

    with transaction.atomic():
        existing = {row.skill_id: row for row in MemberSkill.objects.filter(profile=profile)}
        MemberSkill.objects.filter(profile=profile).exclude(skill_id__in=list(wanted)).delete()
        to_create, to_update = [], []
        for skill_id, level in wanted.items():
            row = existing.get(skill_id)
            if row is None:
                to_create.append(MemberSkill(profile=profile, skill_id=skill_id, level=level))
            elif row.level != level:
                row.level = level
                to_update.append(row)
        if to_create:
            MemberSkill.objects.bulk_create(to_create)
        if to_update:
            MemberSkill.objects.bulk_update(to_update, ['level'])


async def list_skills(company, *, category=None, search=None):
    qs = Skill.objects.filter(company=company)
    if category:
        qs = qs.filter(category__iexact=category)
    if search:
        qs = qs.filter(name__icontains=search)
    return qs.order_by(Lower('category'), Lower('name'))


async def list_professions(company, *, search=None):
    qs = Profession.objects.filter(company=company)
    if search:
        qs = qs.filter(name__icontains=search)
    return qs.order_by(Lower('name'))
