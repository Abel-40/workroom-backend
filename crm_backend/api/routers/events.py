"""Event CRUD + filtered/paginated list API. Structured identically to
api/routers/projects.py (EventIn/EventUpdateIn schemas, an event_data()
response-builder, get_event_for_user -> status-mapped payload)."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from event_management import services
from event_management.models import Event
from ninja import Router, Schema
from pydantic import Field
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['events'])
auth = JWTBearerAuth()

CadenceLiteral = Literal['daily', 'weekly', 'monthly']


class EventIn(Schema):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default='', max_length=10_000)
    event_type_id: UUID | None = None
    department_id: UUID | None = None
    team_id: UUID | None = None
    start_at: datetime
    end_at: datetime | None = None
    location: str = Field(default='', max_length=500)
    attendee_ids: list[UUID] = Field(default_factory=list)
    is_recurring: bool = False
    recurrence_cadence: CadenceLiteral | None = None
    recurrence_days: list[str] = Field(default_factory=list)


class EventUpdateIn(Schema):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    event_type_id: UUID | None = None
    department_id: UUID | None = None
    team_id: UUID | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    location: str | None = Field(default=None, max_length=500)
    attendee_ids: list[UUID] | None = None
    is_recurring: bool | None = None
    recurrence_cadence: CadenceLiteral | None = None
    recurrence_days: list[str] | None = None


async def event_data(event: Event) -> dict:
    attendees = [
        {'id': str(u.id), 'name': u.get_full_name() or u.username}
        async for u in event.attendees.all()
    ]
    return {
        'id': str(event.id),
        'title': event.title,
        'description': event.description,
        'company_id': str(event.company_id),
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
        'is_recurring': event.is_recurring,
        'recurrence_cadence': event.recurrence_cadence or None,
        'recurrence_days': event.recurrence_days,
        'created_at': event.created_at.isoformat(),
        'updated_at': event.updated_at.isoformat(),
    }


@router.post('/', auth=auth, response={201: ApiResponse, 400: ApiResponse})
async def create_event(request, data: EventIn):
    event, error = await services.create_event(
        request.auth, title=data.title, description=data.description,
        event_type_id=data.event_type_id, department_id=data.department_id, team_id=data.team_id,
        start_at=data.start_at, end_at=data.end_at, location=data.location,
        attendee_ids=data.attendee_ids, is_recurring=data.is_recurring,
        recurrence_cadence=data.recurrence_cadence or '', recurrence_days=data.recurrence_days,
    )
    if error == 'no_company':
        return payload('You must belong to a company to create an event.', 400, False)
    if error:
        field = error.removeprefix('invalid_')
        return payload(f'Invalid {field} for this company.', 400, False, errors={error: ['Invalid reference']})
    return payload('Event created successfully.', 201, True, {'event': await event_data(event)})


@router.get('/', auth=auth, response={200: ApiResponse})
async def list_events(
    request, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
    event_type_id: UUID | None = None, department_id: UUID | None = None, team_id: UUID | None = None,
    start_date: date | None = None, end_date: date | None = None, mine: bool = False,
):
    queryset = await services.list_events_for_user(
        request.auth, event_type_id=event_type_id, department_id=department_id, team_id=team_id,
        start_date=start_date, end_date=end_date, mine=mine,
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
    updated, error = await services.update_event(request.auth, event, data.model_dump(exclude_unset=True))
    if error == 'forbidden':
        return payload('You do not have permission to modify this event.', 403, False)
    if error:
        return payload('Invalid reference for this company.', 400, False)
    return payload('Event updated successfully.', 200, True, {'event': await event_data(updated)})


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
