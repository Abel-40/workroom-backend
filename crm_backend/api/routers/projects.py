"""Project CRUD API (Phase 1)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from django.utils import timezone
from ninja import Router, Schema
from projects_and_tasks import services
from projects_and_tasks.models import Project, Task
from pydantic import Field
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['projects'])
auth = JWTBearerAuth()

VisibilityLiteral = Literal['public', 'company', 'department', 'private']
PriorityLiteral = Literal['low', 'medium', 'high']
StatusLiteral = Literal['Active', 'Inactive', 'Done']


class ProjectIn(Schema):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default='No description provided', max_length=10_000)
    department_id: UUID | None = None
    team_id: UUID | None = None
    visibility: VisibilityLiteral = 'company'
    priority: PriorityLiteral = 'medium'
    start_date: datetime | None = None
    deadline: datetime | None = None


class ProjectUpdateIn(Schema):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    department_id: UUID | None = None
    team_id: UUID | None = None
    visibility: VisibilityLiteral | None = None
    priority: PriorityLiteral | None = None
    status: StatusLiteral | None = None
    start_date: datetime | None = None
    deadline: datetime | None = None


async def project_data(project: Project) -> dict:
    """The model's total_tasks/active_tasks/completion_percent properties run
    synchronous ORM queries, so they can't be called from this async view --
    query the counts directly instead."""
    total_tasks = await project.tasks.filter(is_deleted=False).acount()
    completed_tasks = await project.tasks.filter(is_deleted=False, status=Task.STATUS.DONE).acount()
    return {
        'id': str(project.id),
        'title': project.title,
        'description': project.description,
        'company_id': str(project.company_id),
        'department_id': str(project.department_id) if project.department_id else None,
        'team_id': str(project.team_id) if project.team_id else None,
        'visibility': project.visibility,
        'status': project.status,
        'priority': project.priority,
        'start_date': project.start_date.isoformat(),
        'deadline': project.deadline.isoformat(),
        'created_by': str(project.created_by_id) if project.created_by_id else None,
        'created_at': project.created_at.isoformat(),
        'updated_at': project.updated_at.isoformat(),
        'total_tasks': total_tasks,
        'active_tasks': total_tasks - completed_tasks,
        'completion_percent': round((completed_tasks / total_tasks) * 100, 2) if total_tasks else 0,
    }


@router.post('/', auth=auth, response={201: ApiResponse, 400: ApiResponse})
async def create_project(request, data: ProjectIn):
    project, error = await services.create_project(
        request.auth,
        title=data.title, description=data.description,
        department_id=data.department_id, team_id=data.team_id,
        visibility=data.visibility, priority=data.priority,
        start_date=data.start_date or timezone.now(), deadline=data.deadline or timezone.now(),
    )
    if error == 'no_company':
        return payload("You must belong to a company to create a project.", 400, False)
    if error == 'invalid_department':
        return payload('Invalid department for this company.', 400, False, errors={'department_id': ['Invalid department']})
    if error == 'invalid_team':
        return payload('Invalid team for this company.', 400, False, errors={'team_id': ['Invalid team']})
    return payload('Project created successfully.', 201, True, {'project': await project_data(project)})


@router.get('/', auth=auth, response={200: ApiResponse})
async def list_projects(request, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    queryset = await services.list_projects_for_user(request.auth)
    items, meta = await paginate(queryset, page, page_size)
    return payload('Projects retrieved successfully.', 200, True, {
        'results': [await project_data(project) for project in items], 'meta': meta,
    })


@router.get('/{project_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_project(request, project_id: UUID):
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    return payload('Project retrieved successfully.', 200, True, {'project': await project_data(project)})


@router.patch('/{project_id}/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def update_project(request, project_id: UUID, data: ProjectUpdateIn):
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    updated, error = await services.update_project(request.auth, project, data.model_dump(exclude_unset=True))
    if error == 'forbidden':
        return payload('You do not have permission to modify this project.', 403, False)
    if error in ('invalid_department', 'invalid_team'):
        return payload('Invalid department or team for this company.', 400, False)
    return payload('Project updated successfully.', 200, True, {'project': await project_data(updated)})


@router.delete('/{project_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def archive_project(request, project_id: UUID):
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    if not await services.archive_project(request.auth, project):
        return payload('You do not have permission to archive this project.', 403, False)
    return payload('Project archived successfully.', 200, True)
