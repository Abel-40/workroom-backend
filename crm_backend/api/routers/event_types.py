"""Event-type directory API: list a company's own event types (matches
task_types.py's read-only listing pattern), plus creation of a wholly custom
one (name/description only, no default_event_type reference). Enabling
*default* event types lives in api.routers.company_config alongside
departments/task-types (see that router's docstring) -- this router only
owns what's genuinely new: listing and free-form custom creation.
"""

from company.services import get_company_role, get_managed_company, get_member_company
from event_management import services
from event_management.models import EventType
from ninja import Router, Schema
from permissions.catalog import has_permission
from pydantic import Field
from utils.api_response import api_response as payload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['event-types'])
auth = JWTBearerAuth()


class EventTypeIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=1000)


@router.get('/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def list_event_types(request):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    event_types = [
        {'id': item.id, 'name': item.name, 'description': item.description}
        async for item in EventType.objects.filter(company=company).order_by('name')
    ]
    return payload('Event types retrieved successfully.', 200, True, {'results': event_types})


@router.post('/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse})
async def create_event_type(request, data: EventTypeIn):
    company = await get_managed_company(request.auth)
    if company is None:
        return payload("You don't have permission to manage this company's configuration.", 403, False)
    role = await get_company_role(request.auth, company)
    if not has_permission(role, 'event_types:manage'):
        return payload("You don't have permission to manage this company's configuration.", 403, False)
    event_type, error = await services.create_custom_event_type(company, name=data.name, description=data.description)
    if error == 'duplicate_name':
        return payload(
            'An event type with this name already exists.', 400, False,
            errors={'name': ['Already exists for this company']},
        )
    return payload('Event type created successfully.', 201, True, {
        'event_type': {'id': event_type.id, 'name': event_type.name, 'description': event_type.description},
    })
