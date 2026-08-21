"""Business rules and authorization for projects, tasks, and documents.

Every mutation here re-derives authorization from the requesting user's
server-side company/role state (see company.services) rather than trusting
any client-supplied company/project/department id at face value.
"""

import re
from datetime import timedelta

from asgiref.sync import sync_to_async
from company.services import get_company_role, get_member_company, is_company_member
from departments_and_teams.models import Department, Team
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
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


async def list_eligible_assignees(user, project):
    """Users eligible to be assigned a task on this project, scoped by the
    PROJECT's own team/department (not the requester's) -- team takes
    precedence when the project has one; department otherwise; falls back to
    the full company roster when the project has neither, so the feature
    stays usable for unscoped projects rather than returning nothing.
    Department/team `leader` isn't given special treatment here: nothing in
    the existing permission system gates task assignment by leadership."""
    if not await user_can_view_project(user, project):
        return None, 'forbidden'
    if project.team_id:
        candidates = [u async for u in User.objects.filter(team_memberships__id=project.team_id).distinct()]
    elif project.department_id:
        candidates = [
            profile.user async for profile in
            CompanyUserProfile.objects.filter(
                company=project.company, department_id=project.department_id,
            ).select_related('user')
        ]
    else:
        candidates = [
            profile.user async for profile in
            CompanyUserProfile.objects.filter(company=project.company).select_related('user')
        ]
        owner_id = project.company.owner_id
        if owner_id and not any(u.id == owner_id for u in candidates):
            owner = await User.objects.filter(id=owner_id).afirst()
            if owner:
                candidates.append(owner)
    return candidates, None


def _eligible_assignee_ids_sync(project) -> set:
    """Sync mirror of list_eligible_assignees' scoping rule (minus the
    view-permission gate, already satisfied by the caller), for use inside
    persist_ai_generated_tasks -- a sync, transactional function (Django
    transactions are sync-only, see company/services.py)."""
    if project.team_id:
        return set(User.objects.filter(team_memberships__id=project.team_id).values_list('id', flat=True))
    if project.department_id:
        return set(
            CompanyUserProfile.objects.filter(
                company=project.company, department_id=project.department_id,
            ).values_list('user_id', flat=True)
        )
    ids = set(CompanyUserProfile.objects.filter(company=project.company).values_list('user_id', flat=True))
    if project.company.owner_id:
        ids.add(project.company.owner_id)
    return ids


async def is_eligible_assignee(user, project, candidate) -> bool:
    eligible, error = await list_eligible_assignees(user, project)
    if error:
        return False
    return any(u.id == candidate.id for u in eligible)


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


def persist_ai_generated_tasks(generation):
    """Persist a reviewed generation's draft AIGeneratedTask rows as real
    backlog Tasks (source=AI_GENERATED). Runs in one transaction: either the
    whole plan lands or none of it does. A row whose reviewer-assigned user
    turned out ineligible (e.g. the project's department/team changed after
    the row was assigned) is created WITHOUT an assignee rather than failing
    the whole save; its temporary_id is reported back so the caller can
    surface which one needs re-assignment.

    Sync, not async: Django transactions are sync-only (see
    company/services.py), and this is a multi-write flow (Task rows,
    AIGeneratedTask.created_task bookkeeping, AIGeneration.saved_at) that
    must be atomic together.

    Returns (created_tasks, invalid_assignee_temp_ids). Raises ValueError if
    there's nothing to save or the plan was already saved (idempotency: a
    repeated call must not create a second batch of tasks).
    """
    from ai_agent.models import AIGeneratedTask  # local import: projects_and_tasks stays the core app, ai_agent depends on it, not vice versa

    if generation.saved_at is not None:
        raise ValueError('This plan has already been saved.')

    project = generation.project
    draft_rows = list(
        generation.generated_tasks.select_related('suggested_department', 'suggested_task_type', 'assigned_to').order_by('sequence'),
    )
    if not draft_rows:
        raise ValueError('This generation has no draft tasks to save.')

    eligible_ids = _eligible_assignee_ids_sync(project)

    to_create = []
    invalid_assignee_temp_ids = []
    for row in draft_rows:
        assignee = row.assigned_to
        if assignee is not None and assignee.id not in eligible_ids:
            invalid_assignee_temp_ids.append(row.temporary_id)
            assignee = None
        to_create.append(Task(
            project=project, department_id=row.suggested_department_id, task_type_id=row.suggested_task_type_id,
            title=row.title, description=row.description, priority=row.priority, sequence=row.sequence,
            estimated_time=_parse_estimated_effort(row.estimated_effort), assigned_to=assignee,
            created_by=generation.requested_by, source=Task.SOURCE.AI_GENERATED,
            deadline=project.deadline,
        ))

    with transaction.atomic():
        created = Task.objects.bulk_create(to_create)
        for row, task in zip(draft_rows, created):
            row.created_task = task
        AIGeneratedTask.objects.bulk_update(draft_rows, ['created_task'])
        generation.saved_at = timezone.now()
        generation.save(update_fields=['saved_at'])

    for task in created:
        if task.assigned_to_id:
            notify_task_assigned(task)

    return created, invalid_assignee_temp_ids


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
