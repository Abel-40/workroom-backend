"""Business rules and authorization for projects, tasks, and documents.

Every mutation here re-derives authorization from the requesting user's
server-side company/role state (see company.services) rather than trusting
any client-supplied company/project/department id at face value.
"""

import hashlib
import re
from datetime import timedelta
from uuid import UUID

from asgiref.sync import sync_to_async
from audit.services import AuditAction, arecord_event
from company.services import get_company_role, get_member_company, get_member_department_id, is_company_member
from departments_and_teams.models import Department, Team
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from notifications_and_activity.services import (
    log_ownership_transferred,
    log_project_completed,
    log_project_created,
    log_project_reopened,
    notify_project_auto_completed,
    notify_project_completed,
    notify_project_deadline_changed,
    notify_project_ownership_transferred,
    notify_project_reopened,
    notify_task_approved,
    notify_task_assigned,
    notify_task_deadline_changed,
    notify_task_proposal_accepted,
    notify_task_proposal_declined,
    notify_task_proposed,
    notify_task_rejected,
    notify_task_submission_voided,
    notify_task_submitted_for_approval,
    notify_visibility_approved,
    notify_visibility_denied,
    notify_visibility_requested,
)
from users.models import CompanyUserProfile

from .access import AccessLevel, resolve_project_access
from .models import (
    ApprovalRequest, Attachment, DefaultTaskType, Project, ProjectMembership, ProjectVisibilityRequest, Task,
    TaskApproval, TaskDependency, TaskTimeLog, TaskType,
)

User = get_user_model()

PROJECT_UPDATABLE_FIELDS = {'title', 'description', 'visibility', 'priority', 'start_date', 'deadline', 'status'}

# Field-level authority on a task. Which fields a person may write depends on
# what they are to the task, not just on whether they can reach it at all.
#
# The assignee owns how the work gets done: the running description, and their
# own estimate of the effort. (Status is not here -- To Do <-> In Progress has
# its own endpoint, update_task_status, and In Review/Done are reachable only
# through the approval workflow.)
TASK_ASSIGNEE_FIELDS = frozenset({'description', 'estimated_time'})
# Management owns what the work *is* and where it sits.
TASK_MANAGE_FIELDS = frozenset({'title', 'priority', 'department_id', 'task_type_id'})
TASK_UPDATABLE_FIELDS = TASK_ASSIGNEE_FIELDS | TASK_MANAGE_FIELDS
# `deadline` is deliberately in neither set. It has its own endpoint, which
# requires a stated reason, writes an audit row and notifies the assignee --
# see change_task_deadline. Leaving it writable through the general update
# path made all three optional by simply choosing the other endpoint.

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
    """Any access at all. See :func:`projects_and_tasks.access
    .resolve_project_access` for what grants it."""
    return await resolve_project_access(user, project) is not None


async def user_can_manage_project(user, project) -> bool:
    """Edit/archive/assign rights. Kept as a named predicate because thirty
    call sites read better asking this question than comparing an enum, but
    the answer comes from one place."""
    access = await resolve_project_access(user, project)
    return access is not None and access >= AccessLevel.MANAGE


async def user_can_manage_task(user, task) -> bool:
    """Whoever can manage the task's parent project. Nothing else.

    ``task.created_by`` used to grant this on its own, and no longer does --
    the same correction ``created_by`` got on Project, for the same reason. It
    is provenance: it records who raised this piece of work, which is worth
    keeping forever, and it is not a standing claim on the task.

    The baseline made the cost of the old rule concrete: 240 of the 1440
    characterized combinations granted management of a task on a project the
    same person could not open. A Department Leader from another department
    who created a task before the project moved away from them kept authority
    over it indefinitely -- editing, reassigning and archiving work inside a
    department they had since left. That is the manage-without-view shape WP3
    removed from projects, surviving one level down.

    Anyone who genuinely needs continuing authority over a task gets it the
    way everyone else does: a manager membership on its project, which is
    visible and revocable.
    """
    return await user_can_manage_project(user, task.project)


async def user_can_edit_own_task(user, task) -> bool:
    """Whether ``user`` is the assignee doing this work, and still a member.

    Separate from managing the task. The assignee owns *how the work is done*
    -- the description they keep for themselves, their own estimate, their own
    status between To Do and In Progress. They do not own what the work **is**:
    title, priority, type, department, who it belongs to, and when it is due
    stay with whoever can manage the project. See TASK_ASSIGNEE_FIELDS.
    """
    if task.assigned_to_id != user.id:
        return False
    return await is_company_member(user, task.project.company)


async def user_can_update_task_status(user, task) -> bool:
    """Assignee-only, and an active member: Done/In Review are no longer
    reachable through this direct status transition at all (see
    update_task_status) -- they're only reachable via the approval workflow
    below (submit_task_for_approval / approve_task / reject_task_approval)."""
    if task.assigned_to_id != user.id:
        return False
    return await is_company_member(user, task.project.company)


async def user_can_log_time(user, task) -> bool:
    """Logging time is closer to 'doing the work' than the Kanban status
    transitions above (assignee-only, no manager fallback -- see
    user_can_update_task_status) -- a task's creator/project-manager may also
    log time on it, same as editing the task itself (user_can_manage_task)."""
    if task.assigned_to_id == user.id and await is_company_member(user, task.project.company):
        return True
    return await user_can_manage_task(user, task)


async def user_can_delete_time_log(user, log, task) -> bool:
    """The log's author, or whoever can manage the task.

    ``task`` is passed in rather than reached through ``log.task`` on purpose:
    every caller already holds it with ``project__company`` selected, and
    traversing the foreign key here would be a lazy load inside an async
    context -- a runtime error, not a slow query.
    """
    if log.user_id == user.id and await is_company_member(user, task.project.company):
        return True
    return await user_can_manage_task(user, task)


async def user_can_approve_task(user, task) -> bool:
    """Who may approve/reject a task's submitted evidence: the task's
    creator, or -- if that creator has since left the company (created_by
    is NULL after SET_NULL) -- the project's current owner, then the
    project's own creator.

    The fallback chain is unchanged. What is added is that whoever it lands on
    must still be an active member of the company: approving work is a
    company action, and a reference left behind by someone who has gone must
    not authorize it.
    """
    if not await is_company_member(user, task.project.company):
        return False
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
    of its tasks' deadlines (see extend_task_deadline/extend_project_deadline).

    Membership is required as well, so a creator who has left the company
    cannot still move dates in it.
    """
    if project.created_by_id != user.id:
        return False
    return await is_company_member(user, project.company)


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


def _set_collaborators_sync(project, collaborators, *, actor=None):
    """Write the collaborator set to both stores, transactionally.

    ``ProjectMembership(role="contributor")`` is authoritative -- it is what
    ``resolve_project_access`` reads. ``Project.collaborators`` is the
    deprecated M2M, dual-written for one release so a rollback does not lose
    who was on a project. Both writes live here, in one place, so they cannot
    drift; delete the M2M half once the backfill release has shipped.

    Only ``contributor`` rows are touched. A ``viewer`` or ``manager``
    membership was granted deliberately through the members panel and is not
    the collaborator field's to remove.
    """
    keep_ids = {u.id for u in collaborators}
    with transaction.atomic():
        project.collaborators.set(collaborators)
        ProjectMembership.objects.filter(
            project=project, role=ProjectMembership.Role.CONTRIBUTOR,
        ).exclude(user_id__in=keep_ids).delete()
        existing = set(
            ProjectMembership.objects.filter(project=project, user_id__in=keep_ids)
            .values_list('user_id', flat=True)
        )
        ProjectMembership.objects.bulk_create([
            ProjectMembership(
                project=project, user=collaborator, role=ProjectMembership.Role.CONTRIBUTOR, added_by=actor,
            )
            for collaborator in collaborators if collaborator.id not in existing
        ])


_set_collaborators = sync_to_async(_set_collaborators_sync, thread_sensitive=True)


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
    elif visibility == Project.VISIBILITY.PUBLIC:
        # Creation is the other way into `public`, and it has to answer to the
        # same gate as the transition. Checked against the company here since
        # there is no project yet to check against.
        if not company.allow_public_projects:
            return None, 'public_projects_disabled'
        if role not in VISIBILITY_ESCALATION_ROLES[Project.VISIBILITY.PUBLIC]:
            return None, 'visibility_locked'
    elif visibility == Project.VISIBILITY.DEPARTMENT and department_id is None:
        # Same rule as the transition: department visibility with no department
        # is visible to nobody. Refused here too so the two ways into the state
        # cannot disagree.
        return None, 'department_required'
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
        await _set_collaborators(project, collaborators, actor=user)
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
    visibility_change = None
    if 'visibility' in updates and updates['visibility'] != project.visibility:
        allowed, error = await can_set_visibility(user, project, updates['visibility'])
        if not allowed:
            return None, error
        visibility_change = (project.visibility, updates['visibility'])
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
    if visibility_change is not None:
        before, after = visibility_change
        await arecord_event(
            company=project.company, actor=user, action=AuditAction.PROJECT_VISIBILITY_CHANGED, target=project,
            before={'visibility': before}, after={'visibility': after},
        )
    if collaborators is not None:
        await _set_collaborators(project, collaborators, actor=user)
    if project.status == Project.STATUS.DONE and not was_done:
        await sync_to_async(log_project_completed, thread_sensitive=True)(project, user)
        await sync_to_async(notify_project_completed, thread_sensitive=True)(project, user)
    elif was_done and project.status != Project.STATUS.DONE:
        # B8: reopening (creator-only, see forbidden_revert above) had no
        # activity/notification trail at all -- asymmetric with completion.
        await sync_to_async(log_project_reopened, thread_sensitive=True)(project, user)
        await sync_to_async(notify_project_reopened, thread_sensitive=True)(project, user)
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


# --------------------------------------------------------------------------
# Visibility transitions
# --------------------------------------------------------------------------
# Visibility decides who can *find* a project. Three of the four levels keep
# it inside the company; `public` does not, which is why it is the only one
# with a gate of its own.

VISIBILITY_ESCALATION_ROLES = {
    # Anyone who can manage the project may move it between private and
    # department: both stay inside the department that already owns the work.
    Project.VISIBILITY.PRIVATE: None,
    Project.VISIBILITY.DEPARTMENT: None,
    # Company-wide exposure is a department leader's call at minimum.
    Project.VISIBILITY.COMPANY: (
        CompanyUserProfile.Role.Owner,
        CompanyUserProfile.Role.COMPANY_MANAGER,
        CompanyUserProfile.Role.DEPARTMENT_LEADER,
    ),
    # Outside the tenant boundary. Owner or CM only, and only when the company
    # has switched it on.
    Project.VISIBILITY.PUBLIC: (
        CompanyUserProfile.Role.Owner,
        CompanyUserProfile.Role.COMPANY_MANAGER,
    ),
}


async def can_set_visibility(user, project, target_visibility) -> tuple[bool, str | None]:
    """Whether ``user`` may move ``project`` to ``target_visibility``.

    Returns ``(allowed, error)``. Separate from ``user_can_manage_project``
    because managing a project and deciding how far outside itself that
    project is visible are genuinely different questions: the second is about
    the company's exposure, not the project's work.
    """
    if target_visibility == project.visibility:
        return True, None

    if target_visibility == Project.VISIBILITY.PUBLIC and not project.company.allow_public_projects:
        # Checked before the role, so the message a Department Member sees is
        # "this company does not publish projects" rather than "you personally
        # may not" -- the first is true and actionable, the second implies
        # asking someone more senior would help when it would not.
        return False, 'public_projects_disabled'

    if target_visibility == Project.VISIBILITY.DEPARTMENT and not project.department_id:
        # `department` visibility resolves through the project's own department
        # (see access.resolve_project_access). With none set it grants view to
        # nobody, so the project would read as shared to everyone looking at it
        # while behaving exactly like `private`. Refuse rather than create a
        # state whose label and behaviour disagree.
        return False, 'department_required'

    allowed_roles = VISIBILITY_ESCALATION_ROLES.get(target_visibility)
    if allowed_roles is None:
        return True, None

    role = await get_company_role(user, project.company)
    if role not in allowed_roles:
        return False, 'visibility_locked'
    if role == CompanyUserProfile.Role.DEPARTMENT_LEADER:
        own_department_id = await get_member_department_id(user, project.company)
        if not project.department_id or own_department_id != project.department_id:
            return False, 'visibility_locked'
    return True, None


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
    # The reviewer's own authority is re-checked against the target, not
    # inherited from the request. A request records what someone asked for; it
    # never authorizes the change on its own, so a request created when the
    # rules were looser cannot be approved into a state the rules now forbid.
    allowed, error = await can_set_visibility(user, project, request.requested_visibility)
    if not allowed:
        return None, error

    request.status = ProjectVisibilityRequest.STATUS.APPROVED
    request.decided_by = user
    request.decided_at = timezone.now()
    await request.asave(update_fields=['status', 'decided_by', 'decided_at'])
    previous_visibility = project.visibility
    project.visibility = request.requested_visibility
    await project.asave(update_fields=['visibility'])
    await arecord_event(
        company=project.company, actor=user, action=AuditAction.PROJECT_VISIBILITY_CHANGED, target=project,
        before={'visibility': previous_visibility}, after={'visibility': project.visibility},
        reason=f'Approved request {request.id}',
    )
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
    await arecord_event(
        company=project.company, actor=user, action=AuditAction.PROJECT_OWNERSHIP_TRANSFERRED, target=project,
        before={'current_owner': previous_owner}, after={'current_owner': new_owner},
    )
    await sync_to_async(log_ownership_transferred, thread_sensitive=True)(project, user, previous_owner, new_owner)
    await sync_to_async(notify_project_ownership_transferred, thread_sensitive=True)(project, new_owner, user)
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
    """Add a task to a project. Requires MANAGE.

    Project *view* used to be enough, which meant company visibility -- meant
    to grant discovery and nothing else -- let any member of the company add
    work to any project they could find, and decide who it was assigned to.
    That is D1, and it is the last place visibility still conferred a
    capability.

    Contributors are not left without a route: propose_task creates an
    ApprovalRequest that somebody with MANAGE turns into a real task. The two
    landed together deliberately, because tightening this without the
    replacement would have removed the ability to raise work at all.
    """
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'
    if project.status == Project.STATUS.DONE:
        # A new (necessarily not-Done) task would silently break the "all
        # tasks Done" invariant Done itself represents. Reopening has to stay
        # the explicit, creator-only action in update_project (B5).
        return None, 'project_completed'
    if deadline is None:
        # A task that runs to the end of its project is the common case, and
        # making people retype the project's own deadline to express it was
        # friction with no purpose.
        deadline = project.deadline
    if deadline < timezone.now() - PAST_DATE_GRACE:
        return None, 'invalid_deadline'
    if deadline > project.deadline:
        # Inclusive: a task may land exactly on the project deadline. The
        # strict version is what forced the AI generator to subtract an hour
        # from every generated task so its output would validate.
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
    if assignee is not None:
        # Same eligibility scoping as assign_task (B4) -- otherwise a DL/DM
        # could bypass it by setting the assignee at creation instead of
        # through the separate assign endpoint.
        role = await get_company_role(user, project.company)
        if role in DEPARTMENT_SCOPED_ROLES and not await is_eligible_assignee(user, project, assignee):
            return None, 'ineligible_assignee'
    task = await Task.objects.acreate(
        project=project, department=department, task_type=task_type, assigned_to=assignee,
        title=title, description=description, priority=priority, deadline=deadline,
        estimated_time=estimated_time, created_by=user, source=Task.SOURCE.MANUAL,
    )
    if assignee is not None:
        # B8: create_task previously never notified an assignee set at
        # creation time, unlike assign_task's separate reassignment path --
        # inconsistent for what's the same outcome (you were assigned to X).
        await sync_to_async(notify_task_assigned, thread_sensitive=True)(task)
    return task, None


async def update_task(user, task, updates: dict):
    """Apply a partial update, checked field by field.

    Returns (task, error) where error is 'forbidden', 'deadline_has_own_route',
    'invalid_department', 'invalid_task_type', or None.

    Authority is decided against the fields actually being written, not
    against the task as a whole -- otherwise an assignee editing their own
    description would be refused merely because the same endpoint is also the
    one that can rename the task.

    The two sets are not symmetric. TASK_ASSIGNEE_FIELDS is the subset an
    assignee may write; management may write everything, those fields
    included. A request that mixes the sets is refused whole rather than
    part-applied, so the outcome never depends on which fields happened to
    travel together. Unknown fields are dropped, as before.
    """
    if 'deadline' in updates:
        # An explicit refusal rather than a silent drop. Quietly ignoring it
        # would return 200 on a request that changed nothing, and the caller
        # would have no way to tell a no-op from a success.
        return None, 'deadline_has_own_route'

    requested = {field for field in updates if field in TASK_UPDATABLE_FIELDS}
    can_manage = await user_can_manage_task(user, task)
    if not can_manage:
        if requested - TASK_ASSIGNEE_FIELDS:
            return None, 'forbidden'
        if not await user_can_edit_own_task(user, task):
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
    """Move a task to a different assignee, or to nobody.

    A pending submission does not block the reassignment -- it is voided by
    it. Blocking would be the wrong way round: the common reason to reassign
    mid-review is that the original assignee has gone or is stuck, which is
    exactly when their pending submission is least likely to be resolved. A
    task would then be wedged in In Review with nobody able to move it.

    So the submission is closed as VOIDED (never REJECTED -- nobody judged the
    work), the task drops back to In Progress for the new assignee, the person
    who submitted is told their evidence will not be read, and the whole thing
    is audited.
    """
    if not await user_can_manage_task(user, task):
        return None, 'forbidden'
    assignee, error = await _resolve_assignee(task.project.company, assignee_id)
    if error:
        return None, error
    if assignee is not None:
        # list_eligible_assignees' department/team branches are a curated
        # subset (they don't include the Owner/CM unless literally a member
        # of that department), not a security boundary for a manage_any
        # role -- so only DL/DM assignment is actually restricted to it,
        # matching every other department-scoped check this session (B4).
        role = await get_company_role(user, task.project.company)
        if role in DEPARTMENT_SCOPED_ROLES and not await is_eligible_assignee(user, task.project, assignee):
            return None, 'ineligible_assignee'

    previous_assignee_id = task.assigned_to_id
    new_assignee_id = assignee.id if assignee is not None else None
    if previous_assignee_id == new_assignee_id:
        # Reassigning someone to the task they already hold is a no-op, not a
        # reason to void their submission out from under them.
        return task, None

    task.assigned_to = assignee
    await task.asave(update_fields=['assigned_to', 'updated_at'])
    await arecord_event(
        company=task.project.company, actor=user, action=AuditAction.TASK_ASSIGNEE_CHANGED, target=task,
        before={'assigned_to': str(previous_assignee_id) if previous_assignee_id else None},
        after={'assigned_to': str(new_assignee_id) if new_assignee_id else None},
    )
    await _void_pending_approval(task, actor=user)
    if assignee is not None:
        await sync_to_async(notify_task_assigned, thread_sensitive=True)(task)
    return task, None


async def _void_pending_approval(task, *, actor):
    """Close any pending submission on ``task`` because it was reassigned.

    Returns the voided approval, or None if there was none. The task is pulled
    back to In Progress: In Review means "somebody is waiting on a decision",
    and after this nobody is.
    """
    approval = await TaskApproval.objects.select_related('submitted_by').filter(
        task=task, status=TaskApproval.STATUS.PENDING,
    ).afirst()
    if approval is None:
        return None

    # Prime the cached relation: the notification helper reads approval.task,
    # and the caller already holds it with project__company selected.
    approval.task = task
    approval.status = TaskApproval.STATUS.VOIDED
    approval.decided_by = actor
    approval.decided_at = timezone.now()
    await approval.asave(update_fields=['status', 'decided_by', 'decided_at'])

    if task.status == Task.STATUS.IN_REVIEW:
        task.status = Task.STATUS.IN_PROGRESS
        await task.asave(update_fields=['status', 'updated_at'])

    await arecord_event(
        company=task.project.company, actor=actor, action=AuditAction.TASK_APPROVAL_VOIDED, target=task,
        before={'approval_status': TaskApproval.STATUS.PENDING},
        after={'approval_status': TaskApproval.STATUS.VOIDED},
        reason='Task reassigned while a submission was pending',
    )
    # submitted_by is preloaded above: by this point task.assigned_to has
    # already moved, so the recipient cannot be read back off the task.
    await sync_to_async(notify_task_submission_voided, thread_sensitive=True)(
        approval, approval.submitted_by, actor,
    )
    return approval


async def update_task_status(user, task, status):
    """Kanban drag-and-drop transitions for To Do/In Progress only. Done and
    In Review are never reachable here -- they're only reached via the
    approval workflow (submit_task_for_approval sets In Review; approve_task
    sets Done) so an evidence trail always exists behind a completed task.

    Returns (task, error) where error is 'forbidden', 'invalid_status',
    'invalid_transition', or 'blocked'. On 'blocked' the first element is the
    list of unfinished predecessors, so the caller can name them rather than
    saying only that something, somewhere, is not done.
    """
    if not await user_can_update_task_status(user, task):
        return None, 'forbidden'
    if status not in Task.STATUS.values:
        return None, 'invalid_status'
    if status in (Task.STATUS.DONE, Task.STATUS.IN_REVIEW):
        return None, 'invalid_transition'
    if task.status == Task.STATUS.TODO and status != Task.STATUS.TODO:
        # The one thing `blocks` does. Evaluated here, at the transition,
        # against the predecessors' current state -- never read from a flag on
        # the task, which would be stale the instant a predecessor moved.
        blockers = await sync_to_async(blocking_predecessors_sync, thread_sensitive=True)(task)
        if blockers:
            return blockers, 'blocked'
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
# Task dependencies
# --------------------------------------------------------------------------
# Two kinds, `blocks` and `relates_to`, within one project. See the
# TaskDependency docstring for why the list stops there.
#
# `blocks` has exactly one observable consequence: the successor cannot leave
# To Do until the predecessor is Done. It is evaluated at the transition,
# never cached. A stored "is_blocked" flag would be wrong from the instant the
# predecessor moved, and a wrong one either strands somebody on work that is
# ready or waves through work that is not.


def _project_lock_key(project_id) -> int:
    """A stable 63-bit advisory-lock key for one project.

    Postgres advisory locks are a flat integer namespace shared by the whole
    database, so the key has to be derived, not chosen. The project id is the
    right granularity: cycles can only form within a project, so two projects
    never need to wait on each other.
    """
    return int.from_bytes(hashlib.blake2b(project_id.bytes, digest_size=8).digest(), 'big') >> 1


def _blocks_edges_sync(project_id) -> dict:
    """The project's `blocks` graph as {predecessor_id: {successor_id, ...}},
    live tasks only. Small enough to walk in memory -- a project has tasks in
    the hundreds, not the millions -- and reading it in one query beats
    recursing into the database once per edge."""
    graph = {}
    rows = TaskDependency.objects.filter(
        kind=TaskDependency.Kind.BLOCKS,
        predecessor__project_id=project_id, predecessor__is_deleted=False,
        successor__is_deleted=False,
    ).values_list('predecessor_id', 'successor_id')
    for predecessor_id, successor_id in rows:
        graph.setdefault(predecessor_id, set()).add(successor_id)
    return graph


def _path_exists(graph: dict, start, goal) -> bool:
    """Whether `goal` is reachable from `start`. Iterative depth-first: a
    recursive walk would blow the stack on a long chain, and a long chain is
    exactly what somebody building a sequential plan produces."""
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        for neighbour in graph.get(node, ()):
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return False


def _insert_dependency_checked(user, predecessor, successor, kind):
    """Cycle-check and insert one edge, atomically. Returns (dependency, error).

    **Sync, and transactional, on purpose.** This is a read-then-write: it
    walks the existing graph, concludes the new edge is safe, and inserts.
    Two people adding A->B and B->A at the same moment each read a graph
    without the other's edge, each conclude they are safe, and both insert.

    Most races in this codebase leave an inconsistency that something later
    resolves. This one leaves a cycle: two tasks permanently blocking each
    other, with no route out through the product, from data that was valid
    when each request checked it. That is corruption, so it gets a lock.

    Django transactions are sync-only, which is why this is a sync function
    reached through sync_to_async rather than an async one -- the same shape
    persist_ai_generated_tasks and users.services.update_member_role already
    use. Following the existing convention beats inventing a second one.

    Authorization is deliberately **not** checked here. It is a property of
    (user, project) and is not racing with anything in this transaction, so it
    stays in the async caller where `resolve_project_access` lives -- there is
    no sync copy of the access resolver, and adding one to serve this would
    duplicate the single place the access model is defined.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            # Transaction-scoped: released on commit or rollback, so a failure
            # inside the block cannot strand the lock.
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [_project_lock_key(predecessor.project_id)])

        if TaskDependency.objects.filter(
            predecessor=predecessor, successor=successor, kind=kind,
        ).exists():
            return None, 'duplicate'

        if kind == TaskDependency.Kind.BLOCKS:
            # Adding predecessor -> successor closes a cycle exactly when the
            # predecessor is already reachable from the successor.
            graph = _blocks_edges_sync(predecessor.project_id)
            if _path_exists(graph, successor.id, predecessor.id):
                return None, 'cycle'

        dependency = TaskDependency.objects.create(
            predecessor=predecessor, successor=successor, kind=kind, created_by=user,
        )
    return dependency, None


async def add_task_dependency(user, predecessor, successor, kind):
    """Create one edge between two tasks in the same project.

    Returns (dependency, error) where error is 'forbidden',
    'different_projects', 'self_dependency', 'duplicate', or 'cycle'.

    Dependencies shape the work rather than do it, so they follow MANAGE --
    the same authority as creating the tasks they connect.
    """
    if predecessor.project_id != successor.project_id:
        # Cross-project edges are out of scope, and would also make the cycle
        # check unbounded: the graph it walks stops being one project.
        return None, 'different_projects'
    if predecessor.id == successor.id:
        return None, 'self_dependency'
    if not await user_can_manage_project(user, predecessor.project):
        return None, 'forbidden'
    return await sync_to_async(_insert_dependency_checked, thread_sensitive=True)(
        user, predecessor, successor, kind,
    )


def blocking_predecessors_sync(task):
    """The not-yet-Done tasks that must finish before ``task`` may start.

    Live tasks only: an archived predecessor blocks nothing, because there is
    no route left to complete it and it would strand the successor forever.
    """
    return list(
        Task.objects.filter(
            dependents__successor=task,
            dependents__kind=TaskDependency.Kind.BLOCKS,
            is_deleted=False,
        ).exclude(status=Task.STATUS.DONE).distinct()
    )


async def list_task_dependencies(user, task):
    """Both directions for one task: what blocks it, what it blocks, and what
    it merely relates to. Requires view access on the project."""
    if not await user_can_view_project(user, task.project):
        return None, 'forbidden'
    edges = [
        edge async for edge in TaskDependency.objects.filter(
            Q(predecessor=task) | Q(successor=task),
        ).select_related('predecessor', 'successor')
    ]
    return edges, None


async def remove_task_dependency(user, task, dependency_id):
    """Delete one edge. Requires MANAGE, same as creating it."""
    dependency = await TaskDependency.objects.select_related(
        'predecessor', 'predecessor__project', 'predecessor__project__company',
    ).filter(
        Q(predecessor=task) | Q(successor=task), id=dependency_id,
    ).afirst()
    if dependency is None:
        return False, 'not_found'
    if not await user_can_manage_project(user, dependency.predecessor.project):
        return False, 'forbidden'
    await dependency.adelete()
    return True, None


# --------------------------------------------------------------------------
# Task proposals
# --------------------------------------------------------------------------
# Creating a task requires MANAGE. Everyone else proposes one, and somebody
# with MANAGE turns the proposal into real work. A proposal is an
# ApprovalRequest, not a second half-built Task: an unaccepted proposal must
# not appear on the board, count toward progress, or be assignable, and
# anything stored in the tasks table eventually does all three.

TASK_PROPOSAL_TARGET_TYPE = 'projects_and_tasks.project'

# What a proposer may put in the payload. Anything else in a submitted payload
# is dropped rather than stored, so a field that later becomes meaningful
# cannot be smuggled in early by a client that guessed its name.
TASK_PROPOSAL_FIELDS = frozenset({
    'title', 'description', 'priority', 'deadline', 'department_id', 'task_type_id', 'estimated_time',
})


async def _resolve_task_proposal_reviewer(project):
    """Who is told about a proposal on this project.

    Advisory only -- anyone with MANAGE may decide it. The chain exists so a
    request is never created with nobody named on it: the accountable owner
    first, then whoever created the project, then the company owner.
    """
    if project.current_owner_id:
        return await User.objects.filter(id=project.current_owner_id).afirst()
    if project.created_by_id:
        return await User.objects.filter(id=project.created_by_id).afirst()
    return await User.objects.filter(id=project.company.owner_id).afirst()


def _serialize_proposal_payload(payload: dict) -> dict:
    """Reduce a proposal payload to JSON-safe primitives.

    ``deadline`` arrives as a datetime and ``estimated_time`` as a timedelta;
    both have to survive a round trip through a JSONField and come back as the
    types create_task expects.
    """
    clean = {}
    for field, value in payload.items():
        if field not in TASK_PROPOSAL_FIELDS or value is None:
            continue
        if isinstance(value, timedelta):
            clean[field] = value.total_seconds()
        elif hasattr(value, 'isoformat'):
            clean[field] = value.isoformat()
        elif isinstance(value, UUID):
            clean[field] = str(value)
        else:
            clean[field] = value
    return clean


def _deserialize_proposal_payload(payload: dict) -> dict:
    """The inverse. Unknown keys are dropped here too -- a payload written by
    an older release must not be able to reach create_task carrying a field
    this one does not understand."""
    clean = {}
    for field, value in payload.items():
        if field not in TASK_PROPOSAL_FIELDS or value is None:
            continue
        if field == 'deadline':
            parsed = parse_datetime(value) if isinstance(value, str) else value
            if parsed is not None:
                clean[field] = parsed
        elif field == 'estimated_time':
            clean[field] = timedelta(seconds=float(value))
        else:
            clean[field] = value
    return clean


async def propose_task(user, project, **fields):
    """Suggest a task on a project you can contribute to but not manage.

    Returns (request, error) where error is 'forbidden', 'can_create_directly',
    'project_completed', 'invalid_deadline', or None.

    Validation happens twice on purpose: here, so a proposal that could never
    be accepted is refused where somebody can still fix it; and again in
    accept_task_proposal, because the project may have moved underneath it in
    the meantime.
    """
    access = await resolve_project_access(user, project)
    if access is None or access < AccessLevel.CONTRIBUTE:
        return None, 'forbidden'
    if access >= AccessLevel.MANAGE:
        # Not a permission failure -- the opposite. Accepting it silently
        # would leave a manager's proposal queued for the very person who
        # filed it.
        return None, 'can_create_directly'
    if project.status == Project.STATUS.DONE:
        return None, 'project_completed'

    deadline = fields.get('deadline') or project.deadline
    if deadline < timezone.now() - PAST_DATE_GRACE or deadline > project.deadline:
        return None, 'invalid_deadline'
    fields['deadline'] = deadline

    request = await ApprovalRequest.objects.acreate(
        company=project.company,
        kind=ApprovalRequest.Kind.TASK_PROPOSAL,
        target_type=TASK_PROPOSAL_TARGET_TYPE,
        target_id=project.id,
        payload=_serialize_proposal_payload(fields),
        requested_by=user,
        reviewer=await _resolve_task_proposal_reviewer(project),
    )
    await sync_to_async(notify_task_proposed, thread_sensitive=True)(request, project)
    return request, None


async def get_task_proposal_for_user(user, request_id):
    """Tenant scoping only -- authority to decide is checked in accept/decline,
    so a 403 there stays distinguishable from a 404 for something outside the
    caller's company entirely. Same split as get_visibility_request_for_user."""
    company = await get_member_company(user)
    if company is None:
        return None, 'not_found'
    request = await ApprovalRequest.objects.select_related('requested_by').filter(
        id=request_id, company=company, kind=ApprovalRequest.Kind.TASK_PROPOSAL,
    ).afirst()
    if request is None:
        return None, 'not_found'
    return request, None


async def _proposal_project(request):
    """The project a proposal targets, loaded the way every access decision
    needs it. None if it has since been archived or deleted."""
    return await Project.objects.select_related('company', 'department', 'created_by', 'current_owner').filter(
        id=request.target_id, is_deleted=False,
    ).afirst()


async def list_task_proposals_for_user(user, project):
    """Pending proposals on this project, for whoever can manage it.

    A proposer sees their own regardless of access level. They filed it, and
    not being able to see what you asked for is how people ask twice.
    """
    access = await resolve_project_access(user, project)
    if access is None:
        return None, 'forbidden'
    queryset = ApprovalRequest.objects.filter(
        kind=ApprovalRequest.Kind.TASK_PROPOSAL,
        target_type=TASK_PROPOSAL_TARGET_TYPE,
        target_id=project.id,
        status=ApprovalRequest.Status.PENDING,
    ).select_related('requested_by')
    if access < AccessLevel.MANAGE:
        queryset = queryset.filter(requested_by=user)
    return queryset.order_by('-created_at'), None


async def accept_task_proposal(user, request, overrides=None):
    """Turn a proposal into a real task. Requires MANAGE on its project.

    Returns (task, error) where error is 'forbidden', 'not_pending',
    'project_gone', or anything create_task itself returns.

    The task is built by calling create_task with the accepting manager as the
    actor, never by writing the payload into the tasks table. That is the rule
    ApprovalRequest was designed around: approving applies a change through the
    same validated path a direct action takes, so a payload written when the
    rules were looser -- or against a project whose deadline has since moved --
    cannot become a task nobody checked.

    ``created_by`` is therefore the accepting manager, and the proposer stays
    recorded as ``requested_by`` on this request. That split is deliberate:
    created_by heads the approval fallback chain in user_can_approve_task, and
    pointing it at a contributor would make them the approver of work they
    proposed and may well be assigned.
    """
    if request.status != ApprovalRequest.Status.PENDING:
        return None, 'not_pending'
    project = await _proposal_project(request)
    if project is None:
        return None, 'project_gone'
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'

    fields = _deserialize_proposal_payload(request.payload)
    if overrides:
        # A reviewer may correct a proposal as they accept it. Making them
        # retype the whole thing to fix one priority is how a queue stops
        # being used.
        fields.update({k: v for k, v in overrides.items() if k in TASK_PROPOSAL_FIELDS and v is not None})

    task, error = await create_task(
        user, project,
        title=fields.get('title') or 'Untitled task',
        description=fields.get('description') or 'No description provided',
        priority=fields.get('priority') or Task.PRIORITY.MEDIUM,
        deadline=fields.get('deadline'),
        estimated_time=fields.get('estimated_time'),
        department_id=fields.get('department_id'),
        task_type_id=fields.get('task_type_id'),
    )
    if error:
        return None, error

    request.status = ApprovalRequest.Status.APPROVED
    request.decided_by = user
    request.decided_at = timezone.now()
    await request.asave(update_fields=['status', 'decided_by', 'decided_at'])
    await arecord_event(
        company=project.company, actor=user, action=AuditAction.TASK_PROPOSAL_DECIDED, target=task,
        before={'status': ApprovalRequest.Status.PENDING},
        after={'status': ApprovalRequest.Status.APPROVED, 'task': str(task.id)},
        reason=f'Accepted proposal {request.id}',
    )
    await sync_to_async(notify_task_proposal_accepted, thread_sensitive=True)(request, task)
    return task, None


async def decline_task_proposal(user, request, comment=''):
    """Refuse a proposal. Requires MANAGE on its project.

    The comment is optional. A declined proposal with no reason reads as being
    ignored, so the product should push hard for one -- but a reviewer clearing
    an obvious duplicate should not be blocked from doing so, and the
    notification names who declined it, which is enough to start the
    conversation.
    """
    if request.status != ApprovalRequest.Status.PENDING:
        return None, 'not_pending'
    project = await _proposal_project(request)
    if project is None:
        return None, 'project_gone'
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'

    request.status = ApprovalRequest.Status.DENIED
    request.decided_by = user
    request.decided_at = timezone.now()
    request.decision_comment = (comment or '').strip()
    await request.asave(update_fields=['status', 'decided_by', 'decided_at', 'decision_comment'])
    await arecord_event(
        company=project.company, actor=user, action=AuditAction.TASK_PROPOSAL_DECIDED, target=project,
        before={'status': ApprovalRequest.Status.PENDING},
        after={'status': ApprovalRequest.Status.DENIED},
        reason=f'Declined proposal {request.id}',
    )
    await sync_to_async(notify_task_proposal_declined, thread_sensitive=True)(request)
    return request, None



# --------------------------------------------------------------------------
# Time logs -- one real, attributable entry per unit of work, replacing the
# old single overwritable Task.spent_time field (see TaskTimeLog).
# --------------------------------------------------------------------------

MAX_TIME_LOG_HOURS = 24


async def create_time_log(user, task, *, hours: float, work_date=None, description=''):
    """Returns (log, error) where error is 'forbidden', 'invalid_hours', or None."""
    if not await user_can_log_time(user, task):
        return None, 'forbidden'
    if hours is None or hours <= 0 or hours > MAX_TIME_LOG_HOURS:
        return None, 'invalid_hours'
    log = await TaskTimeLog.objects.acreate(
        task=task, user=user, duration=timedelta(hours=hours),
        work_date=work_date or timezone.now().date(), description=(description or '')[:2000],
    )
    return log, None


async def delete_time_log(user, task, log_id):
    """Returns (True, None) / (False, 'not_found') / (False, 'forbidden')."""
    log = await TaskTimeLog.objects.filter(id=log_id, task=task, is_deleted=False).afirst()
    if log is None:
        return False, 'not_found'
    if not await user_can_delete_time_log(user, log, task):
        return False, 'forbidden'
    log.is_deleted = True
    await log.asave(update_fields=['is_deleted'])
    return True, None


async def task_spent_hours(task) -> float | None:
    """Live sum of a task's (non-deleted) time-log entries, in hours -- the
    replacement for the old cached Task.spent_time column. None (not 0) when
    nothing has been logged yet, matching the old field's semantics."""
    result = await TaskTimeLog.objects.filter(task=task, is_deleted=False).aaggregate(total=Sum('duration'))
    total = result['total']
    return round(total.total_seconds() / 3600, 2) if total else None


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
    (an unresolved approval already exists), 'no_evidence',
    'invalid_content_type', 'too_large', 'invalid_page', or None.

    A submission after the task's deadline is allowed and flagged, not
    refused. Refusing it made a task that ran a day late unable to reach Done
    by any route -- a dead end rather than a guardrail, and one that pushed
    people into backdating deadlines to close out real work. Lateness is
    recorded on the approval (``submitted_late``/``late_by``) for the reviewer
    to weigh."""
    # Assignee-only, and an active member of the company -- same rule as
    # user_can_update_task_status, for the same reason: a task reference left
    # pointing at somebody who is no longer here authorizes nothing.
    if not await user_can_update_task_status(user, task):
        return None, 'forbidden'
    if task.status != Task.STATUS.IN_PROGRESS:
        return None, 'invalid_status'
    if await TaskApproval.objects.filter(task=task, status=TaskApproval.STATUS.PENDING).aexists():
        return None, 'already_pending'

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

    now = timezone.now()
    late_by = now - task.deadline if now > task.deadline else None
    approval = await TaskApproval.objects.acreate(
        task=task, submitted_by=user, submitted_late=late_by is not None, late_by=late_by,
    )
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

    await arecord_event(
        company=task.project.company, actor=user, action=AuditAction.TASK_APPROVED, target=task,
        before={'status': Task.STATUS.IN_REVIEW}, after={'status': Task.STATUS.DONE, 'approval': approval.pk},
    )
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

    # The rejection comment is deliberately absent: it is visible only to the
    # submitter (see TaskApproval.rejection_comment), and copying it into a
    # company-scoped audit row would route around that on the day the audit
    # trail grows a UI. The row records that a rejection happened, not what
    # was said.
    await arecord_event(
        company=task.project.company, actor=user, action=AuditAction.TASK_APPROVAL_REJECTED, target=task,
        before={'status': Task.STATUS.IN_REVIEW}, after={'status': Task.STATUS.IN_PROGRESS, 'approval': approval.pk},
    )
    await sync_to_async(notify_task_rejected, thread_sensitive=True)(approval)
    return task, None


async def change_task_deadline(user, task, new_deadline, *, reason):
    """Move a task's deadline, in either direction.

    Belongs to whoever can manage the project rather than to whoever created
    it, requires a stated reason, is audited, and notifies the assignee.
    Returns (task, error) where error is 'forbidden', 'reason_required',
    'exceeds_project_deadline', or None.

    Shortening is allowed. The old rule only let a deadline move outwards,
    which meant the single most common real correction -- "this was scheduled
    optimistically, pull it in" -- had no route through the product at all.
    """
    if not await user_can_manage_project(user, task.project):
        return None, 'forbidden'
    if not reason or not reason.strip():
        return None, 'reason_required'
    if new_deadline > task.project.deadline:
        return None, 'exceeds_project_deadline'
    if new_deadline == task.deadline:
        return task, None

    old_deadline = task.deadline
    task.deadline = new_deadline
    await task.asave(update_fields=['deadline', 'updated_at'])
    await arecord_event(
        company=task.project.company, actor=user, action=AuditAction.TASK_DEADLINE_CHANGED, target=task,
        before={'deadline': old_deadline}, after={'deadline': new_deadline}, reason=reason.strip(),
    )
    await sync_to_async(notify_task_deadline_changed, thread_sensitive=True)(
        task, old_deadline, new_deadline, reason.strip(),
    )
    return task, None


async def change_project_deadline(user, project, new_deadline, *, reason):
    """Move a project's deadline, in either direction.

    Returns (result, error). On 'tasks_exceed_deadline' the result is the list
    of tasks that would be left past the new date, so the caller can show them
    and let someone fix them inline -- the same shape remove_member uses for
    its blockers. Otherwise the result is the project. Other errors are
    'forbidden' and 'reason_required'.

    Shortening is blocked only by the task invariant, never by direction.
    """
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'
    if not reason or not reason.strip():
        return None, 'reason_required'
    if new_deadline == project.deadline:
        return project, None

    if new_deadline < project.deadline:
        offending = [
            task async for task in
            project.tasks.filter(is_deleted=False, deadline__gt=new_deadline).order_by('deadline')
        ]
        if offending:
            return offending, 'tasks_exceed_deadline'

    old_deadline = project.deadline
    project.deadline = new_deadline
    await project.asave(update_fields=['deadline', 'updated_at'])
    await arecord_event(
        company=project.company, actor=user, action=AuditAction.PROJECT_DEADLINE_CHANGED, target=project,
        before={'deadline': old_deadline}, after={'deadline': new_deadline}, reason=reason.strip(),
    )
    await sync_to_async(notify_project_deadline_changed, thread_sensitive=True)(
        project, old_deadline, new_deadline, reason.strip(),
    )
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
            # The project's own deadline. The invariant is inclusive, so a
            # generated task may land exactly on it -- no buffer, and nothing
            # for a human to correct afterwards.
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
