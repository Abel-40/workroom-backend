"""Basic analytics API (Phase 10). Read-only, tenant-scoped."""

from uuid import UUID

from analytics.services import get_company_stats, get_project_stats
from company.services import get_member_company
from ninja import Router
from projects_and_tasks import services
from utils.api_response import api_response as payload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['analytics'])
auth = JWTBearerAuth()


@router.get('/projects/{project_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def project_stats(request, project_id: UUID):
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    return payload('Project analytics retrieved successfully.', 200, True, await get_project_stats(project))


@router.get('/company/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def company_stats(request):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    return payload('Company analytics retrieved successfully.', 200, True, await get_company_stats(company))
