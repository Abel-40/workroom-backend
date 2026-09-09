"""Event CRUD + filtered/paginated list API. Structured identically to
api/routers/projects.py (EventIn/EventUpdateIn schemas, an event_data()
response-builder, get_event_for_user -> status-mapped payload)."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from event_management import services
from event_management.models import Event, EventAttendee
from event_management.recurrence import rrule_from_legacy
from ninja import Router, Schema
from pydantic import Field
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['events'])
auth = JWTBearerAuth()

CadenceLiteral = Literal['daily', 'weekly', 'monthly']
AudienceLiteral = Literal['personal', 'custom', 'team', 'department', 'company']
ResponseLiteral = Literal['no_response', 'accepted', 'declined', 'tentative']

# Errors that mean "this request was structurally wrong" vs. "you may not do
# this". Split explicitly rather than by prefix so adding an error code is a
# decision about its status, not an accident of its name.
AUDIENCE_ERROR_MESSAGES = {
    'forbidden_audience': 'Your role does not allow scheduling an event for that audience.',
    'audience_requires_department': 'A department event must name a department.',
    'audience_requires_team': 'A team event must name a team.',
    'personal_event_has_attendees': 'A personal event cannot have attendees.',
    'invalid_recurrence_rule': 'Recurrence rule is not a valid RRULE.',
}


class EventIn(Schema):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default='', max_length=10_000)
    # Defaults to 'custom' -- not to the model's own 'personal' default, and
    # not to the company-wide behaviour every event used to have. A client
    # that predates this field sends a title, a time and a list of attendees,
    # which is exactly a custom meeting; reading it as 'company' would hand
    # every member a scheduling right the audience matrix just took away, and
    # reading it as 'personal' would silently drop the attendees it sent.
    audience: AudienceLiteral = 'custom'
    event_type_id: UUID | None = None
    department_id: UUID | None = None
    team_id: UUID | None = None
    start_at: datetime
    end_at: datetime | None = None
    location: str = Field(default='', max_length=500)
    attendee_ids: list[UUID] = Field(default_factory=list)
    recurrence_rule: str | None = Field(default=None, max_length=500)
    # DEPRECATED input -- see Event.is_recurring. Still accepted, and
    # translated into recurrence_rule below, so a client that has not moved
    # yet keeps working for one release. recurrence_rule wins if both arrive.
    is_recurring: bool = False
    recurrence_cadence: CadenceLiteral | None = None
    recurrence_days: list[str] = Field(default_factory=list)


class EventUpdateIn(Schema):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    audience: AudienceLiteral | None = None
    event_type_id: UUID | None = None
    department_id: UUID | None = None
    team_id: UUID | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    location: str | None = Field(default=None, max_length=500)
    attendee_ids: list[UUID] | None = None
    recurrence_rule: str | None = Field(default=None, max_length=500)
    is_recurring: bool | None = None
    recurrence_cadence: CadenceLiteral | None = None
    recurrence_days: list[str] | None = None


class AttendeeResponseIn(Schema):
    response: ResponseLiteral


def resolve_recurrence_rule(data) -> str:
    """The one place the deprecated cadence fields are read.

    An explicit ``recurrence_rule`` is authoritative; otherwise the legacy
    trio is translated into the rule that means the same thing. Keeping the
    shim at the HTTP boundary means the service layer only ever sees a rule,
    and deleting the old contract later is a change to this function alone.
    """
    if data.recurrence_rule is not None:
        return data.recurrence_rule
    return rrule_from_legacy(
        is_recurring=bool(data.is_recurring),
        cadence=data.recurrence_cadence or '',
        days=data.recurrence_days or [],
    )


async def event_data(event: Event) -> dict:
    attendees = [
        {
            'id': str(row.user.id),
            'name': row.user.get_full_name() or row.user.username,
            'response': row.response,
            'responded_at': row.responded_at.isoformat() if row.responded_at else None,
        }
        async for row in EventAttendee.objects.filter(event=event).select_related('user').order_by('created_at')
    ]
    return {
        'id': str(event.id),
        'title': event.title,
        'description': event.description,
        'company_id': str(event.company_id),
        'audience': event.audience,
        'event_type_id': str(event.event_type_id) if event.event_type_id else None,
        'event_type_name': event.event_type.name if event.event_type_id else None,
        'department_id': str(event.department_id) if event.department_id else None,
        'department_name': event.department.name if event.department_id else None,
        'team_id': str(event.team_id) if event.team_id else None,
        'team_name': event.team.name if event.team_id else None,
        'organizer_id': str(event.organizer_id) if event.organizer_id else None,
        'organizer_name': (
            (event.organizer.get_full_name() or event.organizer.username) if event.organizer_id else None
        ),
        'attendees': attendees,
        'start_at': event.start_at.isoformat(),
        'end_at': event.end_at.isoformat() if event.end_at else None,
        'location': event.location,
        'recurrence_rule': event.recurrence_rule or None,
        # Deprecated response fields, derived from recurrence_rule. Kept for
        # one release alongside the input shim above.
        'is_recurring': event.is_recurring,
        'recurrence_cadence': event.recurrence_cadence or None,
        'recurrence_days': event.recurrence_days,
        'created_at': event.created_at.isoformat(),
        'updated_at': event.updated_at.isoformat(),
    }


@router.post('/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse})
async def create_event(request, data: EventIn):
    event, error = await services.create_event(
        request.auth, title=data.title, description=data.description, audience=data.audience,
        event_type_id=data.event_type_id, department_id=data.department_id, team_id=data.team_id,
        start_at=data.start_at, end_at=data.end_at, location=data.location,
        attendee_ids=data.attendee_ids, recurrence_rule=resolve_recurrence_rule(data),
    )
    if error == 'no_company':
        return payload('You must belong to a company to create an event.', 400, False)
    if error == 'past_start_at':
        return payload(
            'Events cannot be scheduled in the past.', 400, False,
            errors={'start_at': ['Must be now or in the future']},
        )
    if error == 'forbidden_audience':
        return payload(AUDIENCE_ERROR_MESSAGES[error], 403, False, errors={'audience': ['Not permitted']})
    if error in AUDIENCE_ERROR_MESSAGES:
        return payload(AUDIENCE_ERROR_MESSAGES[error], 400, False, errors={error: ['Invalid request']})
    if error:
        field = error.removeprefix('invalid_')
        return payload(f'Invalid {field} for this company.', 400, False, errors={error: ['Invalid reference']})
    return payload('Event created successfully.', 201, True, {'event': await event_data(event)})


@router.get('/', auth=auth, response={200: ApiResponse})
async def list_events(
    request, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
    event_type_id: UUID | None = None, department_id: UUID | None = None, team_id: UUID | None = None,
    start_date: date | None = None, end_date: date | None = None, mine: bool = False,
    audience: AudienceLiteral | None = None,
):
    queryset = await services.list_events_for_user(
        request.auth, event_type_id=event_type_id, department_id=department_id, team_id=team_id,
        start_date=start_date, end_date=end_date, mine=mine, audience=audience,
    )
    items, meta = await paginate(queryset, page, page_size)
    return payload('Events retrieved successfully.', 200, True, {
        'results': [await event_data(event) for event in items], 'meta': meta,
    })


@router.get('/{event_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_event(request, event_id: UUID):
    event, error = await services.get_event_for_user(request.auth, event_id)
    if error == 'not_found':
        return payload('Event not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this event.', 403, False)
    return payload('Event retrieved successfully.', 200, True, {'event': await event_data(event)})


@router.patch(
    '/{event_id}/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def update_event(request, event_id: UUID, data: EventUpdateIn):
    event, error = await services.get_event_for_user(request.auth, event_id)
    if error == 'not_found':
        return payload('Event not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this event.', 403, False)
    updates = data.model_dump(exclude_unset=True)
    # Collapse the deprecated recurrence trio into the one field the service
    # understands, but only when the request actually mentioned recurrence --
    # an unrelated PATCH must not clear an existing rule.
    legacy_sent = {'is_recurring', 'recurrence_cadence', 'recurrence_days'} & set(updates)
    if 'recurrence_rule' in updates or legacy_sent:
        updates['recurrence_rule'] = resolve_recurrence_rule(data)
    for field in ('is_recurring', 'recurrence_cadence', 'recurrence_days'):
        updates.pop(field, None)
    updated, error = await services.update_event(request.auth, event, updates)
    if error == 'forbidden':
        return payload('You do not have permission to modify this event.', 403, False)
    if error == 'forbidden_audience':
        return payload(AUDIENCE_ERROR_MESSAGES[error], 403, False, errors={'audience': ['Not permitted']})
    if error in AUDIENCE_ERROR_MESSAGES:
        return payload(AUDIENCE_ERROR_MESSAGES[error], 400, False, errors={error: ['Invalid request']})
    if error:
        return payload('Invalid reference for this company.', 400, False)
    return payload('Event updated successfully.', 200, True, {'event': await event_data(updated)})


@router.post(
    '/{event_id}/response/', auth=auth,
    response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def respond_to_event(request, event_id: UUID, data: AttendeeResponseIn):
    """Accept, decline or tentatively accept an invitation.

    Only the invitee themselves: an organizer marking somebody as attending
    would make the field describe the organizer's hope rather than the
    invitee's answer.
    """
    event, error = await services.get_event_for_user(request.auth, event_id)
    if error == 'not_found':
        return payload('Event not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this event.', 403, False)
    _, error = await services.set_attendee_response(request.auth, event, data.response)
    if error == 'not_an_attendee':
        return payload('You are not on the attendee list for this event.', 403, False)
    return payload('Response recorded.', 200, True, {'event': await event_data(event)})


@router.delete('/{event_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def archive_event(request, event_id: UUID):
    event, error = await services.get_event_for_user(request.auth, event_id)
    if error == 'not_found':
        return payload('Event not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this event.', 403, False)
    if not await services.delete_event(request.auth, event):
        return payload('You do not have permission to delete this event.', 403, False)
    return payload('Event deleted successfully.', 200, True)
