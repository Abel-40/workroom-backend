"""Project CRUD API (Phase 1)."""

import mimetypes
from datetime import datetime
from typing import Literal
from uuid import UUID

from ai_agent.models import AIGeneration
from asgiref.sync import sync_to_async
from django.db.models import Count
from django.http import FileResponse
from django.utils import timezone
from ninja import File, Router, Schema
from ninja.files import UploadedFile
from projects_and_tasks import services
from projects_and_tasks.models import Project, Task
from pydantic import Field, HttpUrl
from users.models import CompanyUserProfile
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
    collaborator_ids: list[UUID] = Field(default_factory=list)


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
    collaborator_ids: list[UUID] | None = None


def _project_image_data(project: Project) -> dict | None:
    """A project's cover image is served either as a direct external link, or
    -- for an uploaded file -- through the authenticated streaming endpoint
    below (there is no public /media/ route for user uploads; see
    settings.py), never as a raw file path."""
    if project.image:
        return {'kind': 'upload', 'url': f'/projects/{project.id}/image/'}
    if project.image_url:
        return {'kind': 'link', 'url': project.image_url}
    return None


async def project_data(project: Project) -> dict:
    """The model's total_tasks/active_tasks/completion_percent properties run
    synchronous ORM queries, so they can't be called from this async view --
    query the counts directly instead."""
    total_tasks = await project.tasks.filter(is_deleted=False).acount()
    completed_tasks = await project.tasks.filter(is_deleted=False, status=Task.STATUS.DONE).acount()
    collaborator_ids = [str(user_id) async for user_id in project.collaborators.values_list('id', flat=True)]
    has_saved_plan = await AIGeneration.objects.filter(project=project, saved_at__isnull=False).aexists()
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
        'current_owner_id': str(project.current_owner_id) if project.current_owner_id else None,
        'current_owner_name': project.current_owner.username if project.current_owner_id else None,
        'created_at': project.created_at.isoformat(),
        'updated_at': project.updated_at.isoformat(),
        'total_tasks': total_tasks,
        'active_tasks': total_tasks - completed_tasks,
        'completion_percent': round((completed_tasks / total_tasks) * 100, 2) if total_tasks else 0,
        'collaborator_ids': collaborator_ids,
        'image': _project_image_data(project),
        'has_saved_plan': has_saved_plan,
    }


@router.post('/', auth=auth, response={201: ApiResponse, 400: ApiResponse})
async def create_project(request, data: ProjectIn):
    project, error = await services.create_project(
        request.auth,
        title=data.title, description=data.description,
        department_id=data.department_id, team_id=data.team_id,
        visibility=data.visibility, priority=data.priority,
        start_date=data.start_date or timezone.now(), deadline=data.deadline or timezone.now(),
        collaborator_ids=data.collaborator_ids,
    )
    if error == 'no_company':
        return payload("You must belong to a company to create a project.", 400, False)
    if error == 'invalid_department':
        return payload('Invalid department for this company.', 400, False, errors={'department_id': ['Invalid department']})
    if error == 'invalid_team':
        return payload('Invalid team for this company.', 400, False, errors={'team_id': ['Invalid team']})
    if error == 'invalid_collaborator':
        return payload(
            'Invalid collaborator for this company.', 400, False,
            errors={'collaborator_ids': ['One or more users are not members of this company']},
        )
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
    if error == 'invalid_collaborator':
        return payload(
            'Invalid collaborator for this company.', 400, False,
            errors={'collaborator_ids': ['One or more users are not members of this company']},
        )
    return payload('Project updated successfully.', 200, True, {'project': await project_data(updated)})


class ProjectOwnerIn(Schema):
    new_owner_id: UUID


@router.patch('/{project_id}/owner/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def transfer_project_owner(request, project_id: UUID, data: ProjectOwnerIn):
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    updated, error = await services.transfer_project_ownership(request.auth, project, data.new_owner_id)
    if error == 'forbidden':
        return payload('You do not have permission to transfer this project.', 403, False)
    if error == 'invalid_owner':
        return payload(
            'Invalid owner for this company.', 400, False,
            errors={'new_owner_id': ['Must be a member of this project\'s company']},
        )
    return payload('Project ownership transferred successfully.', 200, True, {'project': await project_data(updated)})


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


class ProjectImageLinkIn(Schema):
    image_url: HttpUrl


@router.put('/{project_id}/image/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def set_project_image_link(request, project_id: UUID, data: ProjectImageLinkIn):
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    updated, error = await services.set_project_image_link(request.auth, project, str(data.image_url))
    if error == 'forbidden':
        return payload('You do not have permission to modify this project.', 403, False)
    return payload('Project image updated successfully.', 200, True, {'project': await project_data(updated)})


@router.post('/{project_id}/image/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def upload_project_image(request, project_id: UUID, image: UploadedFile = File(...)):
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    updated, error = await services.upload_project_image(request.auth, project, image)
    if error == 'forbidden':
        return payload('You do not have permission to modify this project.', 403, False)
    if error == 'too_large':
        return payload('Image exceeds the maximum allowed size (5MB).', 400, False)
    if error == 'invalid_content_type':
        return payload('This image type is not allowed. Use PNG, JPEG, GIF, or WEBP.', 400, False)
    return payload('Project image uploaded successfully.', 200, True, {'project': await project_data(updated)})


@router.get('/{project_id}/image/', auth=auth, response={403: ApiResponse, 404: ApiResponse})
async def download_project_image(request, project_id: UUID):
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    if not project.image:
        return payload('This project has no uploaded image.', 404, False)
    content_type = mimetypes.guess_type(project.image.name)[0] or 'application/octet-stream'
    file_handle = await sync_to_async(project.image.open, thread_sensitive=True)('rb')
    return FileResponse(file_handle, content_type=content_type)


@router.delete('/{project_id}/image/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def delete_project_image(request, project_id: UUID):
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    if not await services.remove_project_image(request.auth, project):
        return payload('You do not have permission to modify this project.', 403, False)
    return payload('Project image removed successfully.', 200, True)


@router.get('/{project_id}/eligible-assignees/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def list_eligible_assignees(request, project_id: UUID):
    """Company members eligible to be assigned a task on this project,
    scoped by the project's own team/department -- used by the @@ mention
    picker and the AI-plan review assignee picker (see projects_and_tasks
    .services.list_eligible_assignees for the scoping rule)."""
    project, error = await services.get_project_for_user(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    candidates, error = await services.list_eligible_assignees(request.auth, project)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)

    # Role/department enrichment mirrors analytics/services.py::get_company_workload's
    # shape (employeeStore's established Employee contract) -- a presentation-layer
    # concern only, so services.list_eligible_assignees itself keeps returning plain
    # User objects unchanged for is_eligible_assignee's sake.
    profiles = {
        profile.user_id: profile
        async for profile in CompanyUserProfile.objects.filter(
            company=project.company, user_id__in=[c.id for c in candidates],
        ).select_related('department')
    }

    def member_data(user):
        if user.id == project.company.owner_id:
            return CompanyUserProfile.Role.Owner, None
        profile = profiles.get(user.id)
        if not profile:
            return None, None
        return profile.role, (profile.department.name if profile.department_id else None)

    # Open (not-Done, not-deleted) task count on THIS project only, so the
    # assignee picker reflects workload on the project being planned rather
    # than a company-wide figure -- one aggregate query, not one per
    # candidate. Presentation-layer only, same as role/department above.
    open_counts: dict[str, int] = {}
    candidate_ids = [c.id for c in candidates]
    if candidate_ids:
        counts_qs = (
            Task.objects.filter(project=project, assigned_to_id__in=candidate_ids, is_deleted=False)
            .exclude(status=Task.STATUS.DONE)
            .values('assigned_to')
            .annotate(count=Count('id'))
        )
        async for row in counts_qs:
            open_counts[str(row['assigned_to'])] = row['count']

    results = []
    for user in candidates:
        role, department = member_data(user)
        results.append({
            'id': str(user.id), 'first_name': user.first_name, 'last_name': user.last_name,
            'username': user.username, 'email': user.email, 'role': role, 'department': department,
            'open_task_count': open_counts.get(str(user.id), 0),
        })
    return payload('Eligible assignees retrieved successfully.', 200, True, {'results': results})
