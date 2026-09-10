"""Business rules and authorization for events and event types.

Mirrors projects_and_tasks/services.py's conventions throughout: every
mutation re-derives authorization from the requesting user's server-side
company/role state (never a client-supplied id), department/team/event-type
references are validated against the caller's own company before being
trusted, and functions return (result, error) tuples rather than raising for
expected business-rule failures.
"""

from asgiref.sync import sync_to_async
from audit.services import AuditAction, arecord_event
from company.services import is_company_member, resolve_company_context
from departments_and_teams.models import Department, Team
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from users.models import CompanyUserProfile

from .models import DefaultEventType, Event, EventAttendee, EventType
from .recurrence import InvalidRecurrenceRule, legacy_from_rrule, validate_rrule

User = get_user_model()

Audience = Event.Audience

EVENT_UPDATABLE_FIELDS = {
    'title', 'description', 'start_at', 'end_at', 'location',
}

# Roles that may schedule for other people. Everything below this line is
# either your own entry (personal) or a meeting you named the attendees of
# (custom).
_COMPANY_ADMIN_ROLES = (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER)


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

async def _is_on_team(user, team_id) -> bool:
    """Whether the user leads or belongs to the team. One query, and never a
    relation traversal -- this runs inside the event loop."""
    if not team_id:
        return False
    return await Team.objects.filter(id=team_id).filter(Q(members=user) | Q(leader=user)).aexists()


def can_create_event_with_audience(context, audience, *, department_id=None, team_id=None,
                                    is_team_lead=False) -> bool:
    """Whether this caller may schedule an event *for* the given audience.

    Scheduling widens as the audience widens, because an event on somebody's
    calendar that they did not ask for is a small claim on their time:

    ``personal``  their own entry -- anyone.
    ``custom``    a meeting with named attendees -- anyone. Ordinary
                  collaboration, not a governance action, which is why it is
                  the one audience a department member can use to involve
                  other people.
    ``team``      Owner/CM, the team's own lead, or the leader of the
                  department the event names.
    ``department``Owner/CM, or that department's leader.
    ``company``   Owner/CM only.

    Takes a resolved ``CompanyContext`` rather than a user so the caller's
    role and department are read once, at the boundary, instead of once per
    branch.
    """
    if audience in (Audience.PERSONAL, Audience.CUSTOM):
        return True
    if context.role in _COMPANY_ADMIN_ROLES:
        return True
    is_department_leader = context.role == CompanyUserProfile.Role.DEPARTMENT_LEADER
    if audience == Audience.DEPARTMENT:
        return is_department_leader and department_id is not None and context.department_id == department_id
    if audience == Audience.TEAM:
        # A Team has no department of its own, so "DL of the owning
        # department" can only be resolved through the department the *event*
        # names. Where it names none, the team's own lead is the whole rule.
        if is_team_lead:
            return True
        return is_department_leader and department_id is not None and context.department_id == department_id
    return False


async def user_can_view_event(user, event) -> bool:
    """Whether the user may see this event at all.

    View follows the audience match, plus one containment rule: anyone who may
    edit or delete an event may also see it, since the alternative is a
    delete button on something the UI cannot render.

    ``personal`` is the exception in both directions -- see
    :func:`user_can_manage_event`.

    Company membership is checked **before** the organizer branch, not after.
    Organizing an event is a per-event grant, and WP3a settled that nothing
    per-event or per-project grants anything to a non-member: removal from a
    company leaves ``organizer`` pointing at the departed user, and their JWT
    still authenticates. Checking membership second would leave a removed
    member reading -- and deleting -- every event they had ever created.
    """
    context = await resolve_company_context(user, company_id=event.company_id)
    if context is None:
        return False
    if event.organizer_id == user.id:
        return True
    if event.audience == Audience.PERSONAL:
        return False
    if _can_manage_shared_event(context, event):
        return True
    if event.audience == Audience.COMPANY:
        return True
    if event.audience == Audience.DEPARTMENT:
        return event.department_id is not None and context.department_id == event.department_id
    if event.audience == Audience.TEAM:
        return await _is_on_team(user, event.team_id)
    if event.audience == Audience.CUSTOM:
        return await EventAttendee.objects.filter(event=event, user=user).aexists()
    return False


def _can_manage_shared_event(context, event) -> bool:
    """The administrative half of manage rights, for any audience but
    ``personal``. Split out so the view rule can reuse it without recursing
    through the organizer check."""
    if event.audience == Audience.PERSONAL:
        return False
    if context.role in _COMPANY_ADMIN_ROLES:
        return True
    if context.role == CompanyUserProfile.Role.DEPARTMENT_LEADER and event.department_id:
        return context.department_id == event.department_id
    return False


async def user_can_manage_event(user, event) -> bool:
    """Edit/delete rights: the organizer, the Owner or a company manager, or
    the leader of the department the event names.

    A ``personal`` event has exactly one manager -- its organizer -- and **no
    administrative override**, deliberately narrower than the rule the brief
    states for events generally. The brief also says a personal event is the
    organizer's own and grants view to nobody else, and an Owner who can
    delete an entry they were never allowed to read is not a coherent
    position. ``documents.Document``'s ``personal`` scope already settles this
    the same way, and this follows it rather than inventing a second answer.

    Membership first, for the reason given in :func:`user_can_view_event`:
    being the organizer is a per-event grant and grants a non-member nothing.
    """
    context = await resolve_company_context(user, company_id=event.company_id)
    return can_manage_event_with_context(user, event, context)


def can_manage_event_with_context(user, event, context) -> bool:
    """:func:`user_can_manage_event` with the company context already resolved.

    The same decision, split out so a list endpoint can resolve the caller's
    context **once** and answer for every row, instead of one
    ``resolve_company_context`` per event. The async version above is the one
    to reach for when you hold a single event and no context; this is the one
    the response builders use.
    """
    if context is None:
        return False
    if event.organizer_id == user.id:
        return True
    if event.audience == Audience.PERSONAL:
        return False
    return _can_manage_shared_event(context, event)


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


def _sync_attendees_sync(event, users, invited_by):
    """Reconcile the attendee list to exactly ``users``.

    Existing rows are left alone rather than replaced, so somebody who has
    already accepted does not lose their response because the organizer added
    a sixth person to the meeting.

    Dual-writes the deprecated ``Event.attendees`` M2M for one release; see
    the field's comment for when that goes.
    """
    wanted = {user.id: user for user in users}
    existing = set(EventAttendee.objects.filter(event=event).values_list('user_id', flat=True))
    EventAttendee.objects.filter(event=event).exclude(user_id__in=wanted).delete()
    EventAttendee.objects.bulk_create([
        EventAttendee(event=event, user=user, invited_by=invited_by)
        for user_id, user in wanted.items() if user_id not in existing
    ])
    event.attendees.set(users)


_sync_attendees = sync_to_async(_sync_attendees_sync, thread_sensitive=True)


def _visible_events_q(user, context) -> Q:
    """The audience half of the list query.

    Written as a single ``Q`` rather than filtered per row because the
    calendar is a paginated list: deciding visibility in Python would mean
    paginating over rows the caller cannot see, and page 2 would come back
    short for no visible reason.
    """
    visible = Q(organizer=user) | Q(audience=Audience.COMPANY)
    if context.role in _COMPANY_ADMIN_ROLES:
        # Manage implies view, and Owner/CM manage every audience but personal.
        return visible | ~Q(audience=Audience.PERSONAL)
    if context.department_id is not None:
        visible |= Q(audience=Audience.DEPARTMENT, department_id=context.department_id)
        if context.role == CompanyUserProfile.Role.DEPARTMENT_LEADER:
            # Same containment rule: a leader may delete any event naming
            # their department, so they have to be able to see it.
            visible |= Q(department_id=context.department_id) & ~Q(audience=Audience.PERSONAL)
    visible |= Q(audience=Audience.TEAM) & (Q(team__members=user) | Q(team__leader=user))
    visible |= Q(audience=Audience.CUSTOM, attendee_records__user=user)
    return visible


async def list_events_for_user(user, *, event_type_id=None, department_id=None, team_id=None,
                                start_date=None, end_date=None, mine=False, audience=None):
    context = await resolve_company_context(user)
    if context is None:
        return Event.objects.none()
    qs = Event.objects.filter(
        company=context.company, is_deleted=False,
    ).filter(_visible_events_q(user, context)).select_related(
        'event_type', 'department', 'team', 'organizer',
    )
    if event_type_id:
        qs = qs.filter(event_type_id=event_type_id)
    if department_id:
        qs = qs.filter(department_id=department_id)
    if team_id:
        qs = qs.filter(team_id=team_id)
    if audience:
        qs = qs.filter(audience=audience)
    if start_date:
        qs = qs.filter(start_at__date__gte=start_date)
    if end_date:
        qs = qs.filter(start_at__date__lte=end_date)
    if mine:
        qs = qs.filter(Q(organizer=user) | Q(attendee_records__user=user))
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


def _check_audience_shape(audience, department, team, attendees) -> str | None:
    """The structural requirements an audience places on the rest of the row.

    An audience that names no group is not a narrower event, it is an event
    nobody can see: ``department`` with no department resolves to an empty
    audience and would silently vanish from every calendar.
    """
    if audience == Audience.DEPARTMENT and department is None:
        return 'audience_requires_department'
    if audience == Audience.TEAM and team is None:
        return 'audience_requires_team'
    if audience == Audience.PERSONAL and attendees:
        return 'personal_event_has_attendees'
    return None


async def create_event(user, *, title, description, start_at, end_at, location,
                        audience=Audience.PERSONAL, event_type_id=None, department_id=None,
                        team_id=None, attendee_ids=None, recurrence_rule=''):
    context = await resolve_company_context(user)
    if context is None:
        return None, 'no_company'
    company = context.company
    if start_at < timezone.now():
        return None, 'past_start_at'
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
    error = _check_audience_shape(audience, department, team, attendees)
    if error:
        return None, error
    if not can_create_event_with_audience(
        context, audience, department_id=department.id if department else None,
        team_id=team.id if team else None,
        is_team_lead=bool(team and team.leader_id == user.id),
    ):
        return None, 'forbidden_audience'
    try:
        rule = validate_rrule(recurrence_rule or '')
    except InvalidRecurrenceRule:
        return None, 'invalid_recurrence_rule'
    event = await Event.objects.acreate(
        title=title, description=description, company=company, audience=audience,
        event_type=event_type, department=department, team=team, start_at=start_at,
        end_at=end_at, location=location, organizer=user, recurrence_rule=rule,
        **legacy_from_rrule(rule),
    )
    if attendees:
        await _sync_attendees(event, attendees, user)
    return event, None


async def update_event(user, event, updates: dict):
    """Returns (event, error) where error is 'forbidden'/'invalid_*'/None.

    Changing the audience is checked twice: manage rights on the event as it
    stands, and creation rights for the audience it is becoming. Otherwise a
    department member could schedule a ``custom`` meeting and immediately
    widen it to ``company``, which is the one thing the creation matrix exists
    to stop.
    """
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
    previous_audience = event.audience
    audience = updates.pop('audience', None) or event.audience
    # The attendee list as it will be *after* this request, so an event cannot
    # be narrowed to personal in the same call that leaves other people on it.
    # Only ``personal`` cares, so only ``personal`` pays for the query.
    resulting_attendees = attendees
    if audience == Audience.PERSONAL and attendees is None:
        resulting_attendees = [row async for row in EventAttendee.objects.filter(event=event)]
    error = _check_audience_shape(audience, event.department, event.team, resulting_attendees)
    if error:
        return None, error
    if audience != previous_audience:
        context = await resolve_company_context(user, company_id=event.company_id)
        if context is None or not can_create_event_with_audience(
            context, audience, department_id=event.department_id, team_id=event.team_id,
            is_team_lead=bool(event.team_id and event.team and event.team.leader_id == user.id),
        ):
            return None, 'forbidden_audience'
        event.audience = audience
    if 'recurrence_rule' in updates:
        try:
            rule = validate_rrule(updates.pop('recurrence_rule') or '')
        except InvalidRecurrenceRule:
            return None, 'invalid_recurrence_rule'
        event.recurrence_rule = rule
        for field, value in legacy_from_rrule(rule).items():
            setattr(event, field, value)
    for field, value in updates.items():
        if field in EVENT_UPDATABLE_FIELDS:
            setattr(event, field, value)
    await event.asave()
    if attendees is not None:
        await _sync_attendees(event, attendees, user)
    if event.audience != previous_audience:
        # Who can see an event is the one change here that cannot be inferred
        # afterwards from the row itself.
        await arecord_event(
            company=event.company, actor=user, action=AuditAction.EVENT_AUDIENCE_CHANGED,
            target=event, before={'audience': previous_audience}, after={'audience': event.audience},
        )
    return event, None


async def delete_event(user, event) -> bool:
    if not await user_can_manage_event(user, event):
        return False
    event.is_deleted = True
    await event.asave(update_fields=['is_deleted', 'updated_at'])
    return True


async def set_attendee_response(user, event, response):
    """Record what an invitee said about an event.

    Only the invitee themselves -- an organizer cannot accept on somebody's
    behalf, which is the whole value of the field. Returns
    (attendee, error) with error 'not_an_attendee' or None.
    """
    attendee = await EventAttendee.objects.filter(event=event, user=user).afirst()
    if attendee is None:
        return None, 'not_an_attendee'
    attendee.response = response
    attendee.responded_at = timezone.now()
    await attendee.asave(update_fields=['response', 'responded_at'])
    return attendee, None
