"""Task-type directory API. Read-only, tenant-scoped: any company member can
list their own company's task types (needed for task creation dropdowns),
matching the existing sector/default-task-type listing endpoints in api.api.
"""

from company.services import get_member_company
from ninja import Router
from projects_and_tasks.models import TaskType
from utils.api_response import api_response as payload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['task-types'])
auth = JWTBearerAuth()


@router.get('/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def list_task_types(request):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    task_types = [
        {'id': item.id, 'name': item.name, 'description': item.description}
        async for item in TaskType.objects.filter(company=company).order_by('name')
    ]
    return payload('Task types retrieved successfully.', 200, True, {'results': task_types})
