"""Business rules and authorization for projects, tasks, and documents.

Every mutation here re-derives authorization from the requesting user's
server-side company/role state (see company.services) rather than trusting
any client-supplied company/project/department id at face value.
"""

import re
import uuid
from datetime import timedelta

from asgiref.sync import sync_to_async
from company.services import get_company_role, get_member_company, is_company_member
from departments_and_teams.models import Department, Team
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from notifications_and_activity.services import notify_task_assigned, notify_task_completed
from users.models import CompanyUserProfile

from .models import Attachment, Project, Task, TaskType

User = get_user_model()

PROJECT_UPDATABLE_FIELDS = {'title', 'description', 'visibility', 'priority', 'start_date', 'deadline', 'status'}
TASK_UPDATABLE_FIELDS = {'title', 'description', 'priority', 'deadline', 'estimated_time', 'spent_time'}

MAX_DOCUMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_DOCUMENT_CONTENT_TYPES = {
    'application/pdf',
    'image/png', 'image/jpeg', 'image/gif', 'image/webp',
    'text/plain', 'text/csv',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/zip',
}


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------

async def user_can_view_project(user, project) -> bool:
    if project.visibility == Project.VISIBILITY.PUBLIC:
        return True
    if project.created_by_id == user.id:
        return True
    role = await get_company_role(user, project.company)
    if role is None:
        return False
    if role in (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER):
        return True
    if project.visibility == Project.VISIBILITY.COMPANY:
        return True
    if project.visibility == Project.VISIBILITY.DEPARTMENT:
        if not project.department_id:
            return False
        profile = await CompanyUserProfile.objects.filter(user=user, company=project.company).afirst()
        return bool(profile and profile.department_id == project.department_id)
    if project.visibility == Project.VISIBILITY.PRIVATE:
        return await project.collaborators.filter(id=user.id).aexists()
    return False


async def user_can_manage_project(user, project) -> bool:
    """Edit/archive rights: creator, company owner, or the leader of the
    project's own department."""
    if project.created_by_id == user.id:
        return True
    role = await get_company_role(user, project.company)
    if role in (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER):
        return True
    if role == CompanyUserProfile.Role.DEPARTMENT_LEADER and project.department_id:
        profile = await CompanyUserProfile.objects.filter(user=user, company=project.company).afirst()
        return bool(profile and profile.department_id == project.department_id)
    return False


async def user_can_manage_task(user, task) -> bool:
    if task.created_by_id == user.id:
        return True
    return await user_can_manage_project(user, task.project)


async def user_can_update_task_status(user, task) -> bool:
    if task.assigned_to_id == user.id:
        return True
    return await user_can_manage_task(user, task)


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------

async def list_projects_for_user(user):
    """Projects within the caller's own company, filtered by visibility.
    Scoped to one company by design: there is no cross-company project
    directory in V1, only direct-link access to a public project by id."""
    company = await get_member_company(user)
    if company is None:
        return Project.objects.none()
    qs = Project.objects.filter(company=company, is_deleted=False).select_related('department', 'created_by')
    role = await get_company_role(user, company)
    if role in (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER):
        return qs.order_by('-created_at')

    visible = Q(visibility=Project.VISIBILITY.PUBLIC) | Q(visibility=Project.VISIBILITY.COMPANY)
    visible |= Q(visibility=Project.VISIBILITY.PRIVATE, created_by=user)
    visible |= Q(visibility=Project.VISIBILITY.PRIVATE, collaborators=user)
    profile = await CompanyUserProfile.objects.filter(user=user, company=company).afirst()
    if profile and profile.department_id:
        visible |= Q(visibility=Project.VISIBILITY.DEPARTMENT, department_id=profile.department_id)
    return qs.filter(visible).distinct().order_by('-created_at')


async def get_project_for_user(user, project_id):
    """Returns (project, error) where error is 'not_found', 'forbidden', or None."""
    project = await Project.objects.select_related('company', 'department', 'created_by').filter(
        id=project_id, is_deleted=False,
    ).afirst()
    if project is None:
        return None, 'not_found'
    if not await user_can_view_project(user, project):
        return None, 'forbidden'
    return project, None


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


async def _resolve_collaborators(company, collaborator_ids):
    """Validates every id belongs to the company (owner or any profile role)
    before it's trusted -- never let a client attach an arbitrary user id to
    a project (Rule 3)."""
    if not collaborator_ids:
        return [], None
    users = [user async for user in User.objects.filter(id__in=collaborator_ids)]
    if len(users) != len(set(collaborator_ids)):
        return None, 'invalid_collaborator'
    for candidate in users:
        if not await is_company_member(candidate, company):
            return None, 'invalid_collaborator'
    return users, None


async def create_project(user, *, title, description, visibility, priority, start_date, deadline,
                          department_id=None, team_id=None, collaborator_ids=None):
    company = await get_member_company(user)
    if company is None:
        return None, 'no_company'
    department, error = await _resolve_department(company, department_id)
    if error:
        return None, error
    team, error = await _resolve_team(company, team_id)
    if error:
        return None, error
    collaborators, error = await _resolve_collaborators(company, collaborator_ids)
    if error:
        return None, error
    project = await Project.objects.acreate(
        title=title, description=description, company=company, department=department, team=team,
        visibility=visibility, priority=priority, start_date=start_date, deadline=deadline, created_by=user,
    )
    if collaborators:
        await sync_to_async(project.collaborators.set, thread_sensitive=True)(collaborators)
    return project, None


async def update_project(user, project, updates: dict):
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'
    if 'department_id' in updates:
        department, error = await _resolve_department(project.company, updates.pop('department_id'))
        if error:
            return None, error
        project.department = department
    if 'team_id' in updates:
        team, error = await _resolve_team(project.company, updates.pop('team_id'))
        if error:
            return None, error
        project.team = team
    collaborators = None
    if 'collaborator_ids' in updates:
        collaborators, error = await _resolve_collaborators(project.company, updates.pop('collaborator_ids'))
        if error:
            return None, error
    for field, value in updates.items():
        if field in PROJECT_UPDATABLE_FIELDS:
            setattr(project, field, value)
    await project.asave()
    if collaborators is not None:
        await sync_to_async(project.collaborators.set, thread_sensitive=True)(collaborators)
    return project, None


async def archive_project(user, project) -> bool:
    if not await user_can_manage_project(user, project):
        return False
    project.is_deleted = True
    await project.asave(update_fields=['is_deleted'])
    return True


# --------------------------------------------------------------------------
# Project cover image -- exactly one of an uploaded file or an external link
# is active at a time (see Project.image / Project.image_url); setting one
# clears the other rather than leaving stale state behind.
# --------------------------------------------------------------------------

MAX_PROJECT_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_PROJECT_IMAGE_CONTENT_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}


async def set_project_image_link(user, project, image_url: str):
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'
    if project.image:
        await sync_to_async(project.image.delete, thread_sensitive=True)(save=False)
    project.image = None
    project.image_url = image_url
    await project.asave(update_fields=['image', 'image_url'])
    return project, None


async def upload_project_image(user, project, uploaded_file):
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'
    if uploaded_file.size > MAX_PROJECT_IMAGE_SIZE_BYTES:
        return None, 'too_large'
    content_type = uploaded_file.content_type or ''
    if content_type not in ALLOWED_PROJECT_IMAGE_CONTENT_TYPES:
        return None, 'invalid_content_type'
    if project.image:
        await sync_to_async(project.image.delete, thread_sensitive=True)(save=False)
    project.image = uploaded_file
    project.image_url = ''
    await project.asave(update_fields=['image', 'image_url'])
    return project, None


async def remove_project_image(user, project) -> bool:
    if not await user_can_manage_project(user, project):
        return False
    if project.image:
        await sync_to_async(project.image.delete, thread_sensitive=True)(save=False)
    project.image = None
    project.image_url = ''
    await project.asave(update_fields=['image', 'image_url'])
    return True


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

async def get_viewable_project(user, project_id):
    """View-only project lookup used to scope task and document endpoints."""
    project = await Project.objects.select_related('company').filter(id=project_id, is_deleted=False).afirst()
    if project is None:
        return None, 'not_found'
    if not await user_can_view_project(user, project):
        return None, 'forbidden'
    return project, None


async def get_task_for_user(user, task_id):
    task = await Task.objects.select_related('project', 'project__company', 'department', 'assigned_to').filter(
        id=task_id, is_deleted=False,
    ).afirst()
    if task is None:
        return None, 'not_found'
    if not await user_can_view_project(user, task.project) and task.assigned_to_id != user.id:
        return None, 'forbidden'
    return task, None


async def _resolve_task_type(company, task_type_id):
    if task_type_id is None:
        return None, None
    task_type = await TaskType.objects.filter(id=task_type_id, company=company).afirst()
    if task_type is None:
        return None, 'invalid_task_type'
    return task_type, None


async def _resolve_assignee(company, assignee_id):
    if assignee_id is None:
        return None, None
    assignee = await User.objects.filter(id=assignee_id).afirst()
    if assignee is None or not await is_company_member(assignee, company):
        return None, 'invalid_assignee'
    return assignee, None


async def create_task(user, project, *, title, description, priority, deadline, estimated_time=None,
                       department_id=None, task_type_id=None, assigned_to_id=None):
    """Any user who can view the project may add tasks to it (matches a
    typical shared-project workflow); editing/archiving the task later still
    requires management rights via user_can_manage_task."""
    if not await user_can_view_project(user, project):
        return None, 'forbidden'
    department, error = await _resolve_department(project.company, department_id)
    if error:
        return None, error
    task_type, error = await _resolve_task_type(project.company, task_type_id)
    if error:
        return None, error
    assignee, error = await _resolve_assignee(project.company, assigned_to_id)
    if error:
        return None, error
    task = await Task.objects.acreate(
        project=project, department=department, task_type=task_type, assigned_to=assignee,
        title=title, description=description, priority=priority, deadline=deadline,
        estimated_time=estimated_time, created_by=user, source=Task.SOURCE.MANUAL,
    )
    return task, None


async def update_task(user, task, updates: dict):
    if not await user_can_manage_task(user, task):
        return None, 'forbidden'
    if 'department_id' in updates:
        department, error = await _resolve_department(task.project.company, updates.pop('department_id'))
        if error:
            return None, error
        task.department = department
    if 'task_type_id' in updates:
        task_type, error = await _resolve_task_type(task.project.company, updates.pop('task_type_id'))
        if error:
            return None, error
        task.task_type = task_type
    for field, value in updates.items():
        if field in TASK_UPDATABLE_FIELDS:
            setattr(task, field, value)
    await task.asave()
    return task, None


async def assign_task(user, task, assignee_id):
    if not await user_can_manage_task(user, task):
        return None, 'forbidden'
    assignee, error = await _resolve_assignee(task.project.company, assignee_id)
    if error:
        return None, error
    task.assigned_to = assignee
    await task.asave(update_fields=['assigned_to', 'updated_at'])
    if assignee is not None:
        await sync_to_async(notify_task_assigned, thread_sensitive=True)(task)
    return task, None


async def update_task_status(user, task, status):
    if not await user_can_update_task_status(user, task):
        return None, 'forbidden'
    if status not in Task.STATUS.values:
        return None, 'invalid_status'
    was_done = task.status == Task.STATUS.DONE
    task.status = status
    await task.asave(update_fields=['status', 'updated_at'])
    if status == Task.STATUS.DONE and not was_done:
        await sync_to_async(notify_task_completed, thread_sensitive=True)(task)
    return task, None


async def archive_task(user, task) -> bool:
    if not await user_can_manage_task(user, task):
        return False
    task.is_deleted = True
    await task.asave(update_fields=['is_deleted'])
    return True


# --------------------------------------------------------------------------
# AI-generated task persistence (Phase 8)
# --------------------------------------------------------------------------

_EFFORT_PATTERN = re.compile(
    r'^\s*(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|d|day|days|m|min|mins|minute|minutes)\s*$', re.IGNORECASE,
)
_EFFORT_UNITS = {
    'h': 'hours', 'hr': 'hours', 'hrs': 'hours', 'hour': 'hours', 'hours': 'hours',
    'd': 'days', 'day': 'days', 'days': 'days',
    'm': 'minutes', 'min': 'minutes', 'mins': 'minutes', 'minute': 'minutes', 'minutes': 'minutes',
}


def _parse_estimated_effort(text: str):
    """'4h' / '2 days' / '30m' -> timedelta, or None if unparseable.

    Effort is auxiliary metadata, not structural: an unparseable value is
    dropped rather than failing the whole generation over a formatting
    quirk in one field.
    """
    if not text:
        return None
    match = _EFFORT_PATTERN.match(text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = _EFFORT_UNITS[match.group(2).lower()]
    return timedelta(**{unit: amount})


def persist_ai_generated_tasks(generation, plan_data: dict) -> int:
    """Validate an AI-generated plan against this company's real
    department/task-type records and persist it as real Tasks
    (source=AI_GENERATED). Runs in one transaction: either the whole plan
    lands or none of it does. Raises ValueError on any structural problem --
    Django re-validates independently of the AI service's own validation
    (Rule 9), and the AI is never allowed to invent a department, task type,
    or assignee that wasn't explicitly supplied to it.

    Sync, not async: called from the sync Celery worker (ai_agent/tasks.py).
    """
    project = generation.project
    tasks_data = plan_data.get('tasks') or []
    if not tasks_data:
        raise ValueError('AI service returned an empty task list.')

    company_department_ids = set(Department.objects.filter(company=project.company).values_list('id', flat=True))
    company_task_type_ids = set(TaskType.objects.filter(company=project.company).values_list('id', flat=True))

    known_temp_ids = set()
    for item in tasks_data:
        if not item.get('temporary_id') or not item.get('title'):
            raise ValueError('AI-generated task is missing temporary_id or title.')
        known_temp_ids.add(item['temporary_id'])

    to_create = []
    for item in tasks_data:
        for dep in item.get('dependency_ids') or []:
            if dep not in known_temp_ids:
                raise ValueError(f"Task '{item['temporary_id']}' references an unknown dependency id.")

        department_id = None
        raw_department_id = item.get('suggested_department_id')
        if raw_department_id:
            department_id = uuid.UUID(str(raw_department_id))
            if department_id not in company_department_ids:
                raise ValueError(f"Task '{item['temporary_id']}' suggested a department outside this company.")

        task_type_id = None
        raw_task_type_id = item.get('suggested_task_type_id')
        if raw_task_type_id:
            task_type_id = uuid.UUID(str(raw_task_type_id))
            if task_type_id not in company_task_type_ids:
                raise ValueError(f"Task '{item['temporary_id']}' suggested a task type outside this company.")

        priority = item.get('priority') or Task.PRIORITY.MEDIUM
        if priority not in Task.PRIORITY.values:
            priority = Task.PRIORITY.MEDIUM

        # assigned_to is intentionally never set here: the AI must not
        # invent an assignee (CLAUDE.md AI Rules / Phase 8 rules).
        to_create.append(Task(
            project=project, department_id=department_id, task_type_id=task_type_id,
            title=item['title'][:255], description=item.get('description') or '',
            priority=priority, sequence=item.get('sequence') or 0,
            estimated_time=_parse_estimated_effort(item.get('estimated_effort') or ''),
            created_by=generation.requested_by, source=Task.SOURCE.AI_GENERATED,
            deadline=project.deadline,
        ))

    with transaction.atomic():
        Task.objects.bulk_create(to_create)
    return len(to_create)


def get_text_document_excerpts(project, *, max_documents=3, max_chars_per_document=3000) -> list[str]:
    """Read the project's own plain-text attachments for the AI assistant's
    document-context capability. Deliberately narrow: no text-extraction
    pipeline exists for any attachment type today (Attachment.file is an
    opaque blob, content_type is metadata only), so this is limited to
    attachments whose content_type is already text/* -- reading those back
    as UTF-8 needs no new dependency. PDF/DOCX extraction is explicitly
    deferred; it would need a new library (e.g. pypdf) for a narrow path.

    Sync, not async: called from the sync Celery worker
    (ai_agent/tasks_assistant.py), same reasoning as persist_ai_generated_tasks.
    """
    attachments = Attachment.objects.filter(
        project=project, is_deleted=False, content_type__startswith='text/',
    ).order_by('-created_at')[:max_documents]

    excerpts = []
    for attachment in attachments:
        if not attachment.file:
            continue
        try:
            with attachment.file.open('rb') as fh:
                raw = fh.read(max_chars_per_document * 4)  # bound the read itself, not just the decode
        except (OSError, ValueError):
            continue
        text = raw.decode('utf-8', errors='replace')[:max_chars_per_document]
        if text.strip():
            excerpts.append(text)
    return excerpts

    return len(to_create)


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

async def upload_document(user, project, uploaded_file, *, label='', task_id=None):
    if not await user_can_view_project(user, project):
        return None, 'forbidden'
    if uploaded_file.size > MAX_DOCUMENT_SIZE_BYTES:
        return None, 'too_large'
    content_type = uploaded_file.content_type or ''
    if content_type not in ALLOWED_DOCUMENT_CONTENT_TYPES:
        return None, 'invalid_content_type'
    task = None
    if task_id is not None:
        task = await Task.objects.filter(id=task_id, project=project).afirst()
        if task is None:
            return None, 'invalid_task'
    attachment = await Attachment.objects.acreate(
        project=project, task=task, uploaded_by=user, type=Attachment.ATTACHMENT_TYPE.FILE,
        file=uploaded_file, name=uploaded_file.name[:255], label=label[:255],
        content_type=content_type, size=uploaded_file.size,
    )
    return attachment, None


async def get_document_for_user(user, document_id):
    document = await Attachment.objects.select_related('project', 'project__company').filter(
        id=document_id, is_deleted=False,
    ).afirst()
    if document is None:
        return None, 'not_found'
    if not await user_can_view_project(user, document.project):
        return None, 'forbidden'
    return document, None


async def delete_document(user, document) -> bool:
    if document.uploaded_by_id != user.id and not await user_can_manage_project(user, document.project):
        return False
    document.is_deleted = True
    await document.asave(update_fields=['is_deleted'])
    return True
