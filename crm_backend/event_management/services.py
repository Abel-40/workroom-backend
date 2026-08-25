"""Business rules and authorization for events and event types.

Mirrors projects_and_tasks/services.py's conventions throughout: every
mutation re-derives authorization from the requesting user's server-side
company/role state (never a client-supplied id), department/team/event-type
references are validated against the caller's own company before being
trusted, and functions return (result, error) tuples rather than raising for
expected business-rule failures.
"""

from asgiref.sync import sync_to_async
from company.services import get_company_role, get_member_company, is_company_member
from departments_and_teams.models import Department, Team
from django.contrib.auth import get_user_model
from django.db.models import Q
from users.models import CompanyUserProfile

from .models import DefaultEventType, Event, EventType

User = get_user_model()

EVENT_UPDATABLE_FIELDS = {
    'title', 'description', 'start_at', 'end_at', 'location',
    'is_recurring', 'recurrence_cadence', 'recurrence_days',
}


# --------------------------------------------------------------------------
# Default / custom event types -- mirrors apply_default_task_types /
# get_default_task_types_with_status (projects_and_tasks/services.py) verbatim.
# --------------------------------------------------------------------------

async def apply_default_event_types(company, *, use_all=False, selected_ids=None):
    if use_all:
        defaults_qs = DefaultEventType.objects.filter(Q(sector_id=company.sector_id) | Q(sector__isnull=True))
    else:
        defaults_qs = DefaultEventType.objects.filter(id__in=selected_ids or [])
    existing_names = {
        name async for name in EventType.objects.filter(company=company).values_list('name', flat=True)
    }
    to_create = [
        EventType(name=item.name, description=item.description, company=company, default_event_type=item)
        async for item in defaults_qs if item.name not in existing_names
    ]
    if to_create:
        await EventType.objects.abulk_create(to_create)
    return to_create


async def get_default_event_types_with_status(company) -> list[dict]:
    enabled_ids = {
        default_id async for default_id in EventType.objects.filter(
            company=company, default_event_type__isnull=False,
        ).values_list('default_event_type_id', flat=True)
    }
    return [
        {'id': str(item.id), 'name': item.name, 'description': item.description, 'enabled': item.id in enabled_ids}
        async for item in DefaultEventType.objects.filter(Q(sector_id=company.sector_id) | Q(sector__isnull=True))
    ]


async def create_custom_event_type(company, *, name, description=''):
    """Returns (event_type, error) where error is 'duplicate_name' or None."""
    if await EventType.objects.filter(company=company, name__iexact=name).aexists():
        return None, 'duplicate_name'
    event_type = await EventType.objects.acreate(name=name, description=description, company=company)
    return event_type, None


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------

async def user_can_view_event(user, event) -> bool:
    role = await get_company_role(user, event.company)
    return role is not None


async def user_can_manage_event(user, event) -> bool:
    """Edit/delete rights: organizer, company owner/manager, or the leader of
    the event's own department -- mirrors user_can_manage_project minus the
    visibility branch (events have no visibility concept, see Event's model
    docstring)."""
    if event.organizer_id == user.id:
        return True
    role = await get_company_role(user, event.company)
    if role in (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER):
        return True
    if role == CompanyUserProfile.Role.DEPARTMENT_LEADER and event.department_id:
        profile = await CompanyUserProfile.objects.filter(user=user, company=event.company).afirst()
        return bool(profile and profile.department_id == event.department_id)
    return False


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

async def _resolve_event_type(company, event_type_id):
    if event_type_id is None:
        return None, None
    event_type = await EventType.objects.filter(id=event_type_id, company=company).afirst()
    if event_type is None:
        return None, 'invalid_event_type'
    return event_type, None


async def _resolve_department(company, department_id):
    if department_id is None:
        return None, None
    department = await Department.objects.filter(id=department_id, company=company).afirst()
    if department is None:
        return None, 'invalid_department'
    return department, None


async def _resolve_team(company, team_id):
    if team_id is None:
        return None, None
    team = await Team.objects.filter(id=team_id, company=company).afirst()
    if team is None:
        return None, 'invalid_team'
    return team, None


async def _resolve_attendees(company, attendee_ids):
    """Validates every id belongs to the company before it's trusted -- never
    let a client attach an arbitrary user id to an event (Rule 3)."""
    if not attendee_ids:
        return [], None
    users = [user async for user in User.objects.filter(id__in=attendee_ids)]
    if len(users) != len(set(attendee_ids)):
        return None, 'invalid_attendee'
    for candidate in users:
        if not await is_company_member(candidate, company):
            return None, 'invalid_attendee'
    return users, None


async def list_events_for_user(user, *, event_type_id=None, department_id=None, team_id=None,
                                start_date=None, end_date=None, mine=False):
    company = await get_member_company(user)
    if company is None:
        return Event.objects.none()
    qs = Event.objects.filter(company=company, is_deleted=False).select_related(
        'event_type', 'department', 'team', 'organizer',
    )
    if event_type_id:
        qs = qs.filter(event_type_id=event_type_id)
    if department_id:
        qs = qs.filter(department_id=department_id)
    if team_id:
        qs = qs.filter(team_id=team_id)
    if start_date:
        qs = qs.filter(start_at__date__gte=start_date)
    if end_date:
        qs = qs.filter(start_at__date__lte=end_date)
    if mine:
        qs = qs.filter(Q(organizer=user) | Q(attendees=user))
    return qs.distinct().order_by('start_at')


async def get_event_for_user(user, event_id):
    """Returns (event, error) where error is 'not_found', 'forbidden', or None."""
    event = await Event.objects.select_related(
        'company', 'event_type', 'department', 'team', 'organizer',
    ).filter(id=event_id, is_deleted=False).afirst()
    if event is None:
        return None, 'not_found'
    if not await user_can_view_event(user, event):
        return None, 'forbidden'
    return event, None


async def create_event(user, *, title, description, start_at, end_at, location,
                        event_type_id=None, department_id=None, team_id=None, attendee_ids=None,
                        is_recurring=False, recurrence_cadence='', recurrence_days=None):
    company = await get_member_company(user)
    if company is None:
        return None, 'no_company'
    event_type, error = await _resolve_event_type(company, event_type_id)
    if error:
        return None, error
    department, error = await _resolve_department(company, department_id)
    if error:
        return None, error
    team, error = await _resolve_team(company, team_id)
    if error:
        return None, error
    attendees, error = await _resolve_attendees(company, attendee_ids)
    if error:
        return None, error
    event = await Event.objects.acreate(
        title=title, description=description, company=company, event_type=event_type,
        department=department, team=team, start_at=start_at, end_at=end_at, location=location,
        organizer=user, is_recurring=is_recurring, recurrence_cadence=recurrence_cadence,
        recurrence_days=recurrence_days or [],
    )
    if attendees:
        await sync_to_async(event.attendees.set, thread_sensitive=True)(attendees)
    return event, None


async def update_event(user, event, updates: dict):
    """Returns (event, error) where error is 'forbidden'/'invalid_*'/None."""
    if not await user_can_manage_event(user, event):
        return None, 'forbidden'
    if 'event_type_id' in updates:
        event_type, error = await _resolve_event_type(event.company, updates.pop('event_type_id'))
        if error:
            return None, error
        event.event_type = event_type
    if 'department_id' in updates:
        department, error = await _resolve_department(event.company, updates.pop('department_id'))
        if error:
            return None, error
        event.department = department
    if 'team_id' in updates:
        team, error = await _resolve_team(event.company, updates.pop('team_id'))
        if error:
            return None, error
        event.team = team
    attendees = None
    if 'attendee_ids' in updates:
        attendees, error = await _resolve_attendees(event.company, updates.pop('attendee_ids'))
        if error:
            return None, error
    for field, value in updates.items():
        if field in EVENT_UPDATABLE_FIELDS:
            setattr(event, field, value)
    await event.asave()
    if attendees is not None:
        await sync_to_async(event.attendees.set, thread_sensitive=True)(attendees)
    return event, None


async def delete_event(user, event) -> bool:
    if not await user_can_manage_event(user, event):
        return False
    event.is_deleted = True
    await event.asave(update_fields=['is_deleted', 'updated_at'])
    return True
