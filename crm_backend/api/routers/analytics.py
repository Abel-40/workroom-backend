"""Analytics API, tiered.

Every endpoint here names the tier it belongs to and checks it. See
`analytics.tiers` for the five tiers and why the boundaries fall where they
do -- the short version is that they are drawn wherever a number stops being
about a project and starts being about a person.
"""

from uuid import UUID

from analytics.services import (
    get_company_stats,
    get_company_workload,
    get_department_stats,
    get_member_workload,
    get_project_people_workload,
    get_project_stats,
)
from analytics.tiers import (
    can_view_company_analytics,
    can_view_department_analytics,
    can_view_project_people,
)
from company.services import get_member_company
from ninja import Router
from projects_and_tasks import services
from utils.api_response import api_response as payload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['analytics'])
auth = JWTBearerAuth()

FORBIDDEN_COMPANY = (
    'Company-wide analytics are available to the company Owner and Company Managers.'
)
FORBIDDEN_DEPARTMENT = (
    'Department analytics are available to that department\'s leader, a Company Manager, or the Owner.'
)


@router.get('/me/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def my_workload(request):
    """PERSONAL tier: your own numbers, available to everyone.

    Never gated. A person being able to see their own workload is not a
    privilege, and making it one would push people toward the company roster
    to answer a question about themselves.
    """
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    workload = await get_member_workload(company, request.auth)
    return payload('Your workload retrieved successfully.', 200, True, workload)


@router.get('/projects/{project_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def project_stats(request, project_id: UUID):
    """PROJECT_AGGREGATE tier: counts and percentages for one project, no
    names, for anyone who can see the project."""
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    return payload('Project analytics retrieved successfully.', 200, True, await get_project_stats(project))


@router.get(
    '/projects/{project_id}/members/', auth=auth,
    response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def project_people(request, project_id: UUID):
    """PROJECT_PEOPLE tier: per-member counts, for this project's members only.

    Project MANAGE. This is where names reappear, and it is bounded by the
    project rather than the company deliberately -- a project manager needs to
    know who on their project is overloaded, which is a different question
    from being able to enumerate everybody.
    """
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    if not await can_view_project_people(request.auth, project):
        return payload('Per-member figures are available to whoever manages this project.', 403, False)
    members = await get_project_people_workload(project)
    return payload('Project member workload retrieved successfully.', 200, True, {'members': members})


@router.get('/company/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def company_stats(request):
    """COMPANY tier: CM and Owner."""
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    if not await can_view_company_analytics(request.auth, company):
        return payload(FORBIDDEN_COMPANY, 403, False)
    return payload('Company analytics retrieved successfully.', 200, True, await get_company_stats(company))


@router.get('/company/members/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def company_workload(request):
    """COMPANY tier: the member roster with per-person counts.

    This is the endpoint §8 singles out. It was readable by every role,
    including a Department Member -- every colleague's name against their open
    task counts, which is one join away from a performance dashboard.
    """
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    if not await can_view_company_analytics(request.auth, company):
        return payload(FORBIDDEN_COMPANY, 403, False)
    members = await get_company_workload(company)
    return payload('Company workload retrieved successfully.', 200, True, {'members': members})


@router.get('/company/departments/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def department_stats(request, department_id: UUID | None = None):
    """DEPARTMENT tier.

    Without a `department_id` this is the breakdown across every department,
    which is a company-tier question -- a Department Leader asking it would be
    reading every other department's numbers through a different door. With
    one, a DL may ask about their own.
    """
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    if not await can_view_department_analytics(request.auth, company, department_id):
        return payload(FORBIDDEN_DEPARTMENT, 403, False)
    departments = await get_department_stats(company)
    if department_id is not None:
        departments = [row for row in departments if row.get('id') == str(department_id)]
    return payload('Department analytics retrieved successfully.', 200, True, {'departments': departments})
