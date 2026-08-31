"""Business rules and authorization for projects, tasks, and documents.

Every mutation here re-derives authorization from the requesting user's
server-side company/role state (see company.services) rather than trusting
any client-supplied company/project/department id at face value.
"""

import re
from datetime import timedelta

from asgiref.sync import sync_to_async
from company.services import get_company_role, get_member_company, get_member_department_id, is_company_member
from departments_and_teams.models import Department, Team
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from notifications_and_activity.services import (
    log_ownership_transferred,
    log_project_completed,
    log_project_created,
    notify_project_auto_completed,
    notify_project_deadline_extended,
    notify_task_approved,
    notify_task_assigned,
    notify_task_deadline_extended,
    notify_task_rejected,
    notify_task_submitted_for_approval,
    notify_visibility_approved,
    notify_visibility_denied,
    notify_visibility_requested,
)
from users.models import CompanyUserProfile

from .models import Attachment, DefaultTaskType, Project, ProjectVisibilityRequest, Task, TaskApproval, TaskType

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
    """Edit/archive rights: creator, current owner, company owner, or the
    leader of the project's own department."""
    if project.created_by_id == user.id or project.current_owner_id == user.id:
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
    """Assignee-only: Done/In Review are no longer reachable through this
    direct status transition at all (see update_task_status) -- they're
    only reachable via the approval workflow below (submit_task_for_approval
    / approve_task / reject_task_approval)."""
    return task.assigned_to_id == user.id


async def user_can_approve_task(user, task) -> bool:
    """Who may approve/reject a task's submitted evidence: the task's
    creator, or -- if that creator has since left the company (created_by
    is NULL after SET_NULL) -- the project's current owner, then the
    project's own creator."""
    if task.created_by_id is not None:
        return task.created_by_id == user.id
    project = task.project
    if project.current_owner_id is not None:
        return project.current_owner_id == user.id
    return project.created_by_id == user.id


async def user_can_extend_deadline(user, project) -> bool:
    """Deadline extension is narrower than user_can_manage_project: only the
    project's creator qualifies -- current owner, company owner/manager, and
    department leader do not. Used for both a project's own deadline and any
    of its tasks' deadlines (see extend_task_deadline/extend_project_deadline)."""
    return project.created_by_id == user.id


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
    qs = Project.objects.filter(company=company, is_deleted=False).select_related('department', 'created_by', 'current_owner')
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
    project = await Project.objects.select_related('company', 'department', 'created_by', 'current_owner').filter(
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


DEPARTMENT_SCOPED_ROLES = (CompanyUserProfile.Role.DEPARTMENT_LEADER, CompanyUserProfile.Role.DEPARTMENT_MEMBER)

# A client clock a little ahead, or a start_date left to default to the
# router's own timezone.now() a moment before this function runs, must not
# get spuriously rejected as "in the past" -- only a date meaningfully
# earlier than now counts.
PAST_DATE_GRACE = timedelta(minutes=1)


async def create_project(user, *, title, description, visibility, priority, start_date, deadline,
                          department_id=None, team_id=None, collaborator_ids=None):
    company = await get_member_company(user)
    if company is None:
        return None, 'no_company'
    now = timezone.now()
    if start_date < now - PAST_DATE_GRACE:
        return None, 'invalid_start_date'
    if deadline < now - PAST_DATE_GRACE:
        return None, 'invalid_deadline'
    role = await get_company_role(user, company)
    if role in DEPARTMENT_SCOPED_ROLES:
        own_department_id = await get_member_department_id(user, company)
        if department_id != own_department_id:
            return None, 'department_locked'
    if role == CompanyUserProfile.Role.DEPARTMENT_MEMBER:
        # A DM-created project starts private regardless of what visibility
        # was requested -- department/company visibility is only reachable
        # afterward through request_visibility_change (department) or a
        # Department Leader/Owner/CM raising it directly (company). See A7.
        visibility = Project.VISIBILITY.PRIVATE
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
        visibility=visibility, priority=priority, start_date=start_date, deadline=deadline,
        created_by=user, current_owner=user,
    )
    if collaborators:
        await sync_to_async(project.collaborators.set, thread_sensitive=True)(collaborators)
    await sync_to_async(log_project_created, thread_sensitive=True)(project)
    return project, None


async def update_project(user, project, updates: dict):
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'
    role = await get_company_role(user, project.company)
    if 'department_id' in updates and role in DEPARTMENT_SCOPED_ROLES:
        # A DL/DM's own department is fixed at creation (see create_project)
        # and stays fixed afterward too -- only Owner/CM may move a project
        # to a different department post-creation.
        return None, 'department_locked'
    if (
        'visibility' in updates and role == CompanyUserProfile.Role.DEPARTMENT_MEMBER
        and updates['visibility'] != project.visibility
    ):
        # A DM can't change visibility directly at all, even downward --
        # only request_visibility_change (department) or a Department
        # Leader/Owner/CM acting directly (company) may move it. See A7.
        return None, 'visibility_locked'
    was_done = project.status == Project.STATUS.DONE
    if 'status' in updates:
        new_status = updates['status']
        if new_status == Project.STATUS.DONE and not was_done:
            total = await project.tasks.filter(is_deleted=False).acount()
            if total == 0:
                return None, 'no_tasks'
            if await project.tasks.filter(is_deleted=False).exclude(status=Task.STATUS.DONE).aexists():
                return None, 'tasks_incomplete'
        if was_done and new_status != Project.STATUS.DONE and user.id != project.created_by_id:
            return None, 'forbidden_revert'
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
    if project.status == Project.STATUS.DONE and not was_done:
        await sync_to_async(log_project_completed, thread_sensitive=True)(project)
    return project, None


# --------------------------------------------------------------------------
# Visibility escalation (A7) -- a Department Member's project starts private
# (see create_project) and can't be raised directly by them (see
# update_project's visibility_locked check). This is their only path to
# department visibility; company visibility is reachable only by a
# Department Leader/Owner/CM acting directly through update_project, never
# through this request/approval cycle.
# --------------------------------------------------------------------------

async def _resolve_visibility_reviewer(project):
    """The department's own leader, or -- if it currently has none -- the
    company owner, so a request is never left unreviewable by anyone."""
    if project.department_id:
        department = await Department.objects.select_related('leader').filter(id=project.department_id).afirst()
        if department is not None and department.leader_id is not None:
            return department.leader
    return await User.objects.filter(id=project.company.owner_id).afirst()


async def _can_review_visibility_request(user, project) -> bool:
    """Same department-scoping as _can_manage_this_department: Owner/CM may
    review any request, a Department Leader only their own department's."""
    role = await get_company_role(user, project.company)
    if role in (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER):
        return True
    if role == CompanyUserProfile.Role.DEPARTMENT_LEADER and project.department_id:
        own_department_id = await get_member_department_id(user, project.company)
        return own_department_id == project.department_id
    return False


async def request_visibility_change(user, project, target_visibility):
    """A Department Member requests their own private project be raised to
    department visibility. Returns (request, error) where error is
    'forbidden', 'invalid_target', 'already_pending', or None."""
    if project.created_by_id != user.id:
        return None, 'forbidden'
    role = await get_company_role(user, project.company)
    if role != CompanyUserProfile.Role.DEPARTMENT_MEMBER:
        return None, 'forbidden'
    if target_visibility != Project.VISIBILITY.DEPARTMENT:
        return None, 'invalid_target'
    if project.visibility != Project.VISIBILITY.PRIVATE or project.department_id is None:
        return None, 'invalid_target'
    try:
        request = await ProjectVisibilityRequest.objects.acreate(
            project=project, requested_by=user, requested_visibility=target_visibility,
        )
    except IntegrityError:
        return None, 'already_pending'
    reviewer = await _resolve_visibility_reviewer(project)
    await sync_to_async(notify_visibility_requested, thread_sensitive=True)(request, reviewer)
    return request, None


async def list_visibility_requests_for_user(user):
    """Pending visibility requests the caller may review: Owner/CM see every
    pending request company-wide; a Department Leader sees only their own
    department's; anyone else sees none."""
    company = await get_member_company(user)
    if company is None:
        return ProjectVisibilityRequest.objects.none()
    role = await get_company_role(user, company)
    qs = ProjectVisibilityRequest.objects.filter(
        project__company=company, project__is_deleted=False, status=ProjectVisibilityRequest.STATUS.PENDING,
    ).select_related('project', 'requested_by')
    if role in (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER):
        return qs.order_by('-created_at')
    if role == CompanyUserProfile.Role.DEPARTMENT_LEADER:
        own_department_id = await get_member_department_id(user, company)
        if own_department_id is None:
            return ProjectVisibilityRequest.objects.none()
        return qs.filter(project__department_id=own_department_id).order_by('-created_at')
    return ProjectVisibilityRequest.objects.none()


async def get_visibility_request_for_user(user, request_id):
    """Returns (request, error) where error is 'not_found' or None. Tenant
    scoping only here -- review-authority scoping happens in
    approve/deny_visibility_request so a 403 there is distinguishable from a
    404 for something outside the caller's company entirely."""
    company = await get_member_company(user)
    if company is None:
        return None, 'not_found'
    request = await ProjectVisibilityRequest.objects.select_related(
        'project', 'project__company', 'project__department', 'requested_by',
    ).filter(id=request_id, project__company=company).afirst()
    if request is None:
        return None, 'not_found'
    return request, None


async def approve_visibility_request(user, request):
    """Returns (request, error) where error is 'forbidden', 'not_pending', or
    None. Raises the project straight to department visibility."""
    if request.status != ProjectVisibilityRequest.STATUS.PENDING:
        return None, 'not_pending'
    project = request.project
    if not await _can_review_visibility_request(user, project):
        return None, 'forbidden'
    request.status = ProjectVisibilityRequest.STATUS.APPROVED
    request.decided_by = user
    request.decided_at = timezone.now()
    await request.asave(update_fields=['status', 'decided_by', 'decided_at'])
    project.visibility = request.requested_visibility
    await project.asave(update_fields=['visibility'])
    await sync_to_async(notify_visibility_approved, thread_sensitive=True)(request)
    return request, None


async def deny_visibility_request(user, request, comment=''):
    """Returns (request, error) where error is 'forbidden', 'not_pending', or
    None."""
    if request.status != ProjectVisibilityRequest.STATUS.PENDING:
        return None, 'not_pending'
    project = request.project
    if not await _can_review_visibility_request(user, project):
        return None, 'forbidden'
    request.status = ProjectVisibilityRequest.STATUS.DENIED
    request.decided_by = user
    request.decided_at = timezone.now()
    request.decision_comment = comment
    await request.asave(update_fields=['status', 'decided_by', 'decided_at', 'decision_comment'])
    await sync_to_async(notify_visibility_denied, thread_sensitive=True)(request)
    return request, None


async def archive_project(user, project) -> bool:
    if not await user_can_manage_project(user, project):
        return False
    project.is_deleted = True
    await project.asave(update_fields=['is_deleted'])
    return True


async def transfer_project_ownership(user, project, new_owner_id):
    """Reassign the project's current owner. Never touches the immutable
    created_by (see Project.created_by) -- history is preserved by design.
    Returns (project, error) where error is 'forbidden', 'invalid_owner', or
    None."""
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'
    if new_owner_id == project.current_owner_id:
        return project, None
    new_owner = await User.objects.filter(id=new_owner_id).afirst()
    if new_owner is None or not await is_company_member(new_owner, project.company):
        return None, 'invalid_owner'
    previous_owner = project.current_owner
    project.current_owner = new_owner
    await project.asave(update_fields=['current_owner'])
    await sync_to_async(log_ownership_transferred, thread_sensitive=True)(project, user, previous_owner, new_owner)
    return project, None


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
    # project__is_deleted=False: an archived project's tasks are otherwise
    # still individually reachable (and mutable) by a direct task id, since
    # archive_project never cascades is_deleted onto its tasks -- see B2.
    task = await Task.objects.select_related('project', 'project__company', 'department', 'assigned_to').filter(
        id=task_id, is_deleted=False, project__is_deleted=False,
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
    if deadline < timezone.now() - PAST_DATE_GRACE:
        return None, 'invalid_deadline'
    if deadline >= project.deadline:
        return None, 'invalid_deadline'
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
    if 'deadline' in updates and updates['deadline'] >= task.project.deadline:
        return None, 'invalid_deadline'
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
    """Kanban drag-and-drop transitions for To Do/In Progress only. Done and
    In Review are never reachable here -- they're only reached via the
    approval workflow (submit_task_for_approval sets In Review; approve_task
    sets Done) so an evidence trail always exists behind a completed task."""
    if not await user_can_update_task_status(user, task):
        return None, 'forbidden'
    if status not in Task.STATUS.values:
        return None, 'invalid_status'
    if status in (Task.STATUS.DONE, Task.STATUS.IN_REVIEW):
        return None, 'invalid_transition'
    task.status = status
    await task.asave(update_fields=['status', 'updated_at'])
    return task, None


async def archive_task(user, task) -> bool:
    if not await user_can_manage_task(user, task):
        return False
    task.is_deleted = True
    await task.asave(update_fields=['is_deleted'])
    return True


# --------------------------------------------------------------------------
# Task approval workflow -- evidence submission, approve/reject, and the
# deadline-extension actions that are narrower than general project
# management (see user_can_approve_task/user_can_extend_deadline above).
# --------------------------------------------------------------------------

def _maybe_auto_complete_project(project):
    """Flips a project to Done automatically once every one of its own
    (non-deleted) tasks is Done. Never fires for a zero-task project -- an
    empty project can never be considered "complete," matching the same
    rule update_project enforces for a manual Done set. Sync, not async:
    called via sync_to_async from approve_task, same convention as the rest
    of this module's notification/activity side-effects."""
    if project.status == Project.STATUS.DONE:
        return
    tasks_qs = Task.objects.filter(project=project, is_deleted=False)
    if tasks_qs.count() == 0:
        return
    if tasks_qs.exclude(status=Task.STATUS.DONE).exists():
        return
    project.status = Project.STATUS.DONE
    project.save(update_fields=['status', 'updated_at'])
    log_project_completed(project)
    notify_project_auto_completed(project)


async def submit_task_for_approval(user, task, *, files=None, links=None, page_ids=None):
    """Assignee-only: submits evidence (any mix of uploaded files, external
    links, and Info Portal pages) for the task's approver to review. Moves
    the task to In Review. Returns (approval, error) where error is one of
    'forbidden', 'invalid_status' (task isn't In Progress), 'already_pending'
    (an unresolved approval already exists), 'deadline_passed', 'no_evidence',
    'invalid_content_type', 'too_large', 'invalid_page', or None."""
    if task.assigned_to_id != user.id:
        return None, 'forbidden'
    if task.status != Task.STATUS.IN_PROGRESS:
        return None, 'invalid_status'
    if await TaskApproval.objects.filter(task=task, status=TaskApproval.STATUS.PENDING).aexists():
        return None, 'already_pending'
    if task.deadline < timezone.now():
        return None, 'deadline_passed'

    files = files or []
    links = links or []
    page_ids = page_ids or []
    if not files and not links and not page_ids:
        return None, 'no_evidence'

    for uploaded_file in files:
        if uploaded_file.size > MAX_DOCUMENT_SIZE_BYTES:
            return None, 'too_large'
        if (uploaded_file.content_type or '') not in ALLOWED_DOCUMENT_CONTENT_TYPES:
            return None, 'invalid_content_type'

    pages = []
    if page_ids:
        from pages.models import Page  # local import: projects_and_tasks stays independent of pages at module load

        pages = [
            page async for page in
            Page.objects.filter(id__in=page_ids, folder__company=task.project.company, is_deleted=False)
        ]
        if len(pages) != len(set(page_ids)):
            return None, 'invalid_page'

    approval = await TaskApproval.objects.acreate(task=task, submitted_by=user)
    to_create = []
    for uploaded_file in files:
        to_create.append(Attachment(
            project=task.project, task=task, approval=approval, uploaded_by=user,
            type=Attachment.ATTACHMENT_TYPE.FILE, file=uploaded_file, name=uploaded_file.name[:255],
            content_type=uploaded_file.content_type or '', size=uploaded_file.size,
        ))
    for url in links:
        to_create.append(Attachment(
            project=task.project, task=task, approval=approval, uploaded_by=user,
            type=Attachment.ATTACHMENT_TYPE.LINK, url=url, name=str(url)[:255],
        ))
    for page in pages:
        to_create.append(Attachment(
            project=task.project, task=task, approval=approval, uploaded_by=user,
            type=Attachment.ATTACHMENT_TYPE.PAGE, page=page, name=page.title[:255],
        ))
    if to_create:
        await Attachment.objects.abulk_create(to_create)

    task.status = Task.STATUS.IN_REVIEW
    await task.asave(update_fields=['status', 'updated_at'])
    await sync_to_async(notify_task_submitted_for_approval, thread_sensitive=True)(approval)
    return approval, None


async def approve_task(user, task):
    """Approver-only (see user_can_approve_task). Sets the task Done and
    auto-completes the parent project when eligible. Returns (task, error)
    where error is 'forbidden', 'no_pending_approval', or None."""
    if not await user_can_approve_task(user, task):
        return None, 'forbidden'
    approval = await TaskApproval.objects.filter(
        task=task, status=TaskApproval.STATUS.PENDING,
    ).order_by('-submitted_at').afirst()
    if approval is None:
        return None, 'no_pending_approval'

    approval.status = TaskApproval.STATUS.APPROVED
    approval.decided_by = user
    approval.decided_at = timezone.now()
    await approval.asave(update_fields=['status', 'decided_by', 'decided_at'])

    task.status = Task.STATUS.DONE
    await task.asave(update_fields=['status', 'updated_at'])

    await sync_to_async(notify_task_approved, thread_sensitive=True)(approval)
    await sync_to_async(_maybe_auto_complete_project, thread_sensitive=True)(task.project)
    return task, None


async def reject_task_approval(user, task, comment: str):
    """Approver-only. A rejection comment is required and is visible only to
    the original submitter -- enforced in the API serialization layer (see
    api.routers.tasks), never returned to anyone else. Sets the task back to
    In Progress so the assignee can rework and resubmit. Returns
    (task, error) where error is 'forbidden', 'comment_required',
    'no_pending_approval', or None."""
    if not await user_can_approve_task(user, task):
        return None, 'forbidden'
    if not comment or not comment.strip():
        return None, 'comment_required'
    approval = await TaskApproval.objects.filter(
        task=task, status=TaskApproval.STATUS.PENDING,
    ).order_by('-submitted_at').afirst()
    if approval is None:
        return None, 'no_pending_approval'

    approval.status = TaskApproval.STATUS.REJECTED
    approval.decided_by = user
    approval.decided_at = timezone.now()
    approval.rejection_comment = comment
    await approval.asave(update_fields=['status', 'decided_by', 'decided_at', 'rejection_comment'])

    task.status = Task.STATUS.IN_PROGRESS
    await task.asave(update_fields=['status', 'updated_at'])

    await sync_to_async(notify_task_rejected, thread_sensitive=True)(approval)
    return task, None


async def extend_task_deadline(user, task, new_deadline):
    """Project-creator-only (narrower than general project management -- see
    user_can_extend_deadline), extend-only: the new deadline must be later
    than the task's current one, and must still land strictly before the
    project's own deadline (the same invariant enforced at task creation).
    Returns (task, error) where error is 'forbidden', 'not_an_extension',
    'exceeds_project_deadline', or None."""
    if not await user_can_extend_deadline(user, task.project):
        return None, 'forbidden'
    if new_deadline <= task.deadline:
        return None, 'not_an_extension'
    if new_deadline >= task.project.deadline:
        return None, 'exceeds_project_deadline'
    old_deadline = task.deadline
    task.deadline = new_deadline
    await task.asave(update_fields=['deadline', 'updated_at'])
    await sync_to_async(notify_task_deadline_extended, thread_sensitive=True)(task, old_deadline, new_deadline)
    return task, None


async def extend_project_deadline(user, project, new_deadline):
    """Project-creator-only, extend-only -- see extend_task_deadline above
    for the matching task-level action. Returns (project, error) where
    error is 'forbidden', 'not_an_extension', or None."""
    if not await user_can_extend_deadline(user, project):
        return None, 'forbidden'
    if new_deadline <= project.deadline:
        return None, 'not_an_extension'
    old_deadline = project.deadline
    project.deadline = new_deadline
    await project.asave(update_fields=['deadline', 'updated_at'])
    await sync_to_async(notify_project_deadline_extended, thread_sensitive=True)(project, old_deadline, new_deadline)
    return project, None


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
        generation.generated_tasks.select_related(
            'suggested_department', 'suggested_task_type', 'assigned_to', 'suggested_assignee',
        ).order_by('sequence'),
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
        # No human override -- fall back to the AI's suggestion, but only if
        # that person is still eligible right now (eligibility may have
        # changed since the suggestion was made, e.g. a team/department
        # reassignment on the project).
        if assignee is None and row.suggested_assignee_id and row.suggested_assignee_id in eligible_ids:
            assignee = row.suggested_assignee
        to_create.append(Task(
            project=project, department_id=row.suggested_department_id, task_type_id=row.suggested_task_type_id,
            title=row.title, description=row.description, priority=row.priority, sequence=row.sequence,
            estimated_time=_parse_estimated_effort(row.estimated_effort), assigned_to=assignee,
            created_by=generation.requested_by, source=Task.SOURCE.AI_GENERATED,
            # Must be strictly before the project's own deadline (see
            # create_task/update_task's matching validation for
            # manually-created tasks) -- an hour's buffer is a safe default
            # a human can extend later via extend_task_deadline.
            deadline=project.deadline - timedelta(hours=1),
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
    # project__is_deleted=False: see get_task_for_user's comment -- the same
    # gap applies to documents (B2).
    document = await Attachment.objects.select_related('project', 'project__company').filter(
        id=document_id, is_deleted=False, project__is_deleted=False,
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


# --------------------------------------------------------------------------
# Default-task-type configuration -- mirrors departments_and_teams.services'
# apply_default_departments/get_default_departments_with_status, shared by
# the onboarding wizard (api.api.create_task_types_from_defaults) and the
# post-registration company-config management endpoints.
# --------------------------------------------------------------------------

async def apply_default_task_types(company, *, use_all=False, selected_ids=None):
    """Creates company TaskType rows from DefaultTaskType templates for
    ``company``'s sector (or explicit ``selected_ids``), skipping any whose
    name already exists in this company. Returns the list of newly created
    TaskType instances (empty if everything was already present)."""
    defaults = DefaultTaskType.objects.filter(
        Q(sector_id=company.sector_id) | Q(sector__isnull=True),
    ) if use_all else DefaultTaskType.objects.filter(id__in=selected_ids or [])
    existing_names = {
        name async for name in TaskType.objects.filter(company=company).values_list('name', flat=True)
    }
    to_create = [
        TaskType(name=item.name, description=item.description, company=company, default_task_type=item)
        async for item in defaults if item.name not in existing_names
    ]
    if to_create:
        await TaskType.objects.abulk_create(to_create)
    return to_create


async def get_default_task_types_with_status(company) -> list[dict]:
    """Every DefaultTaskType available to ``company``'s sector, annotated
    with whether it's already enabled -- see
    departments_and_teams.services.get_default_departments_with_status for
    the same pattern."""
    enabled_ids = {
        default_id async for default_id in TaskType.objects.filter(
            company=company, default_task_type__isnull=False,
        ).values_list('default_task_type_id', flat=True)
    }
    return [
        {'id': str(item.id), 'name': item.name, 'description': item.description, 'enabled': item.id in enabled_ids}
        async for item in DefaultTaskType.objects.filter(Q(sector_id=company.sector_id) | Q(sector__isnull=True))
    ]
