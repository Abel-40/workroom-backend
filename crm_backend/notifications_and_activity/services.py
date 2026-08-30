"""Notification creation helpers.

Plain sync Django ORM calls: called directly from sync contexts (Celery
tasks, accept_invite_in_transaction) and via sync_to_async from async
contexts (projects_and_tasks.services), matching the sync_to_async
convention already used throughout this codebase for the same reason.
"""

from .models import CompanyActivity, Notification

# Task assignment and AI-generation failure are the two genuinely
# time-sensitive/actionable notification types -- these always email
# regardless of the recipient's preference. Everything else is optional and
# respects CompanyUserProfile.email_notifications_enabled.
TYPE_CATEGORY = {
    Notification.Type.TASK_ASSIGNED: Notification.Category.CRITICAL,
    Notification.Type.AI_GENERATION_FAILED: Notification.Category.CRITICAL,
    Notification.Type.TASK_COMPLETED: Notification.Category.OPTIONAL,
    Notification.Type.INVITATION_ACCEPTED: Notification.Category.OPTIONAL,
    Notification.Type.AI_GENERATION_COMPLETED: Notification.Category.OPTIONAL,
    # Both put the ball back in someone's court -- an approver has evidence
    # waiting, an assignee has rework to do -- same actionable/time-sensitive
    # posture as TASK_ASSIGNED above.
    Notification.Type.TASK_SUBMITTED_FOR_APPROVAL: Notification.Category.CRITICAL,
    Notification.Type.TASK_REJECTED: Notification.Category.CRITICAL,
    Notification.Type.TASK_APPROVED: Notification.Category.OPTIONAL,
    Notification.Type.DEADLINE_EXTENDED: Notification.Category.OPTIONAL,
    Notification.Type.PROJECT_AUTO_COMPLETED: Notification.Category.OPTIONAL,
    # A pending request blocking someone else's work is actionable/time-sensitive;
    # the requester learning the outcome is not.
    Notification.Type.VISIBILITY_REQUESTED: Notification.Category.CRITICAL,
    Notification.Type.VISIBILITY_APPROVED: Notification.Category.OPTIONAL,
    Notification.Type.VISIBILITY_DENIED: Notification.Category.OPTIONAL,
}


def _should_email(recipient, category: str) -> bool:
    if category == Notification.Category.CRITICAL:
        return True
    from users.models import CompanyUserProfile

    enabled = CompanyUserProfile.objects.filter(user=recipient).values_list(
        'email_notifications_enabled', flat=True,
    ).first()
    # No profile row -- e.g. the company owner -- defaults to enabled.
    return True if enabled is None else enabled


def _maybe_enqueue_email(notification):
    if not _should_email(notification.recipient, notification.category):
        return
    # Local import: notifications_and_activity.tasks imports celery's
    # shared_task decorator at module load, which would otherwise create a
    # load-order dependency on Celery being configured before this module is
    # first imported (same local-import convention already used elsewhere in
    # this codebase for cross-module Celery task references).
    from .tasks import send_notification_email_task

    send_notification_email_task.delay(str(notification.id))


def _create(recipient, type_, title, message='', related_object_type='', related_object_id=None):
    if recipient is None:
        return None
    notification = Notification.objects.create(
        recipient=recipient, type=type_, category=TYPE_CATEGORY.get(type_, Notification.Category.OPTIONAL),
        title=title, message=message,
        related_object_type=related_object_type, related_object_id=related_object_id,
    )
    _maybe_enqueue_email(notification)
    return notification


def notify_task_assigned(task):
    if task.assigned_to_id is None:
        return
    _create(
        task.assigned_to, Notification.Type.TASK_ASSIGNED,
        f"You were assigned to '{task.title}'", related_object_type='task', related_object_id=task.id,
    )


def notify_task_completed(task):
    # Don't notify someone that they completed their own task.
    if task.created_by_id is None or task.created_by_id == task.assigned_to_id:
        return
    _create(
        task.created_by, Notification.Type.TASK_COMPLETED,
        f"Task '{task.title}' was completed", related_object_type='task', related_object_id=task.id,
    )


def notify_invitation_accepted(user, company):
    _create(
        user, Notification.Type.INVITATION_ACCEPTED,
        f"You've joined {company.name}", related_object_type='company', related_object_id=company.id,
    )


def notify_ai_generation_completed(generation):
    _create(
        generation.requested_by, Notification.Type.AI_GENERATION_COMPLETED,
        f"AI plan ready for '{generation.project.title}'",
        message=f'{generation.task_count} tasks generated.',
        related_object_type='ai_generation', related_object_id=generation.id,
    )


def notify_ai_generation_failed(generation):
    # Provider responses can contain infrastructure details, JSON bodies, or
    # exception text. Those remain in the generation record and worker logs
    # for diagnosis, but must never be copied to a user-facing notification.
    _create(
        generation.requested_by, Notification.Type.AI_GENERATION_FAILED,
        f"AI plan failed for '{generation.project.title}'",
        message='We could not create this AI plan right now. Please try again in a few minutes.',
        related_object_type='ai_generation', related_object_id=generation.id,
    )


def notify_task_submitted_for_approval(approval):
    """Tells the task's approver (see projects_and_tasks.services
    .user_can_approve_task for who that is) that evidence is waiting for
    their review."""
    task = approval.task
    approver_id = task.created_by_id or task.project.current_owner_id or task.project.created_by_id
    if approver_id is None:
        return
    from users.models import User

    approver = User.objects.filter(id=approver_id).first()
    _create(
        approver, Notification.Type.TASK_SUBMITTED_FOR_APPROVAL,
        f"'{task.title}' was submitted for your approval",
        related_object_type='task', related_object_id=task.id,
    )


def notify_task_approved(approval):
    """Tells the assignee who submitted the evidence that it was approved
    and the task is now Done."""
    if approval.submitted_by_id is None:
        return
    task = approval.task
    _create(
        approval.submitted_by, Notification.Type.TASK_APPROVED,
        f"Your submission for '{task.title}' was approved",
        related_object_type='task', related_object_id=task.id,
    )


def notify_task_rejected(approval):
    """Tells the assignee who submitted the evidence that it was rejected
    and the task is back in progress. The rejection comment itself is
    deliberately NOT included here -- it's returned only via the
    GET /tasks/{id}/approvals/ endpoint, and only to this same recipient
    (see api.routers.tasks' redaction rule)."""
    if approval.submitted_by_id is None:
        return
    task = approval.task
    _create(
        approval.submitted_by, Notification.Type.TASK_REJECTED,
        f"Your submission for '{task.title}' needs changes",
        related_object_type='task', related_object_id=task.id,
    )


def notify_task_deadline_extended(task, old_deadline, new_deadline):
    """Tells the assignee their task's deadline moved."""
    if task.assigned_to_id is None:
        return
    _create(
        task.assigned_to, Notification.Type.DEADLINE_EXTENDED,
        f"Deadline extended for '{task.title}'",
        message=f"New deadline: {new_deadline.isoformat()}.",
        related_object_type='task', related_object_id=task.id,
    )


def notify_project_deadline_extended(project, old_deadline, new_deadline):
    """Tells the project's current owner (the person accountable for
    delivery day-to-day) its deadline moved."""
    if project.current_owner_id is None:
        return
    _create(
        project.current_owner, Notification.Type.DEADLINE_EXTENDED,
        f"Deadline extended for '{project.title}'",
        message=f"New deadline: {new_deadline.isoformat()}.",
        related_object_type='project', related_object_id=project.id,
    )


def notify_project_auto_completed(project):
    """Tells the project's current owner that every task landed Done and the
    project was automatically marked complete."""
    if project.current_owner_id is None:
        return
    _create(
        project.current_owner, Notification.Type.PROJECT_AUTO_COMPLETED,
        f"Project '{project.title}' was automatically marked complete",
        message='Every task in this project is now Done.',
        related_object_type='project', related_object_id=project.id,
    )


def notify_visibility_requested(request, reviewer):
    """Tells the resolved reviewer (the department's leader, or -- if it has
    none -- the company owner; see projects_and_tasks.services
    .request_visibility_change) a Department Member is waiting on a
    department-visibility decision. No stable FK for "who should review
    this" exists on the request itself, since the department's leader can
    change after the request is filed -- the caller resolves it fresh."""
    if reviewer is None:
        return
    project = request.project
    _create(
        reviewer, Notification.Type.VISIBILITY_REQUESTED,
        f"'{project.title}' requests department visibility",
        message=f'Requested by {_actor_name(request.requested_by)}.',
        related_object_type='project', related_object_id=project.id,
    )


def notify_visibility_approved(request):
    """Tells the requester their department-visibility request was approved."""
    if request.requested_by_id is None:
        return
    _create(
        request.requested_by, Notification.Type.VISIBILITY_APPROVED,
        f"'{request.project.title}' is now visible to your department",
        related_object_type='project', related_object_id=request.project_id,
    )


def notify_visibility_denied(request):
    """Tells the requester their department-visibility request was denied."""
    if request.requested_by_id is None:
        return
    _create(
        request.requested_by, Notification.Type.VISIBILITY_DENIED,
        f"Your visibility request for '{request.project.title}' was denied",
        message=request.decision_comment or '',
        related_object_type='project', related_object_id=request.project_id,
    )


# --------------------------------------------------------------------------
# Company activity feed -- a curated, company-wide event log. Deliberately
# not one entry per minor field edit; only the ActivityType values below are
# logged (see CompanyActivity.ActivityType).
# --------------------------------------------------------------------------

def _actor_name(user) -> str:
    if user is None:
        return 'Someone'
    return user.username or user.email


def log_activity(company, actor, type_, summary, related_object_type='', related_object_id=None):
    return CompanyActivity.objects.create(
        company=company, actor=actor, type=type_, summary=summary,
        related_object_type=related_object_type, related_object_id=related_object_id,
    )


def log_project_created(project):
    log_activity(
        project.company, project.created_by, CompanyActivity.ActivityType.PROJECT_CREATED,
        f"{_actor_name(project.created_by)} created project '{project.title}'",
        related_object_type='project', related_object_id=project.id,
    )


def log_project_completed(project):
    log_activity(
        project.company, project.current_owner, CompanyActivity.ActivityType.PROJECT_COMPLETED,
        f"Project '{project.title}' was completed",
        related_object_type='project', related_object_id=project.id,
    )


def log_ownership_transferred(project, actor, previous_owner, new_owner):
    # actor is whoever performed the transfer (already permission-checked by
    # transfer_project_ownership) -- distinct from new_owner, who is only the
    # recipient and may have no manage rights at all. Recording new_owner as
    # the actor here used to misattribute the action to them, making the
    # activity feed look like they did something they may not even have
    # permission to do.
    log_activity(
        project.company, actor, CompanyActivity.ActivityType.PROJECT_OWNERSHIP_TRANSFERRED,
        f"Ownership of '{project.title}' moved from {_actor_name(previous_owner)} to {_actor_name(new_owner)}",
        related_object_type='project', related_object_id=project.id,
    )


def log_member_invited(company, actor, email):
    log_activity(
        company, actor, CompanyActivity.ActivityType.MEMBER_INVITED,
        f'{email} was invited to join the company',
    )


def log_member_joined(user, company):
    log_activity(
        company, user, CompanyActivity.ActivityType.MEMBER_JOINED,
        f'{_actor_name(user)} joined the company',
    )


def log_member_removed(company, actor, removed_user, reassigned_count=0):
    summary = f'{_actor_name(removed_user)} was removed from the company'
    if reassigned_count:
        summary += f' ({reassigned_count} project(s)/task(s) reassigned)'
    log_activity(company, actor, CompanyActivity.ActivityType.MEMBER_REMOVED, summary)


def log_department_created(department, actor):
    log_activity(
        department.company, actor, CompanyActivity.ActivityType.DEPARTMENT_CREATED,
        f"Department '{department.name}' was created",
        related_object_type='department', related_object_id=department.id,
    )


def log_team_created(team, actor):
    log_activity(
        team.company, actor, CompanyActivity.ActivityType.TEAM_CREATED,
        f"Team '{team.name}' was created",
        related_object_type='team', related_object_id=team.id,
    )
