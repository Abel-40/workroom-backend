"""Post-registration company default-configuration management.

The onboarding wizard (api.api.create_departments_from_defaults /
create_task_types_from_defaults) lets an Owner enable default
departments/task-types once, at signup. This router is the same
capability made available afterward, so an Owner isn't forced to manually
recreate a default they skipped, or hit duplicate-name conflicts trying to.
Both routers call the exact same departments_and_teams.services
.apply_default_departments / projects_and_tasks.services
.apply_default_task_types functions -- one dedupe-by-name implementation,
not two.
"""

from uuid import UUID

from company.services import get_managed_company
from departments_and_teams.services import apply_default_departments, get_default_departments_with_status
from ninja import Router, Schema
from projects_and_tasks.services import apply_default_task_types, get_default_task_types_with_status
from pydantic import Field
from utils.api_response import api_response as payload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['company-config'])
auth = JWTBearerAuth()


class DefaultSelectionIn(Schema):
    selected_ids: list[UUID] = Field(default_factory=list)
    use_all: bool = False


@router.get('/', auth=auth, response={200: ApiResponse, 403: ApiResponse})
async def list_default_config(request):
    company = await get_managed_company(request.auth)
    if company is None:
        return payload("You don't have permission to manage this company's configuration.", 403, False)
    return payload('Default configuration retrieved successfully.', 200, True, {
        'departments': await get_default_departments_with_status(company),
        'task_types': await get_default_task_types_with_status(company),
    })


@router.post('/departments/', auth=auth, response={201: ApiResponse, 403: ApiResponse})
async def enable_default_departments(request, data: DefaultSelectionIn):
    company = await get_managed_company(request.auth)
    if company is None:
        return payload("You don't have permission to manage this company's configuration.", 403, False)
    created = await apply_default_departments(company, use_all=data.use_all, selected_ids=data.selected_ids)
    return payload('Departments enabled successfully.', 201, True, {
        'created_departments': [{'name': item.name} for item in created],
        'total_created': len(created),
    })


@router.post('/task-types/', auth=auth, response={201: ApiResponse, 403: ApiResponse})
async def enable_default_task_types(request, data: DefaultSelectionIn):
    company = await get_managed_company(request.auth)
    if company is None:
        return payload("You don't have permission to manage this company's configuration.", 403, False)
    created = await apply_default_task_types(company, use_all=data.use_all, selected_ids=data.selected_ids)
    return payload('Task types enabled successfully.', 201, True, {
        'created_task_types': [{'name': item.name} for item in created],
        'total_created': len(created),
    })
