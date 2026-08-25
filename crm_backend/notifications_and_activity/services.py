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


def log_ownership_transferred(project, previous_owner, new_owner):
    log_activity(
        project.company, new_owner, CompanyActivity.ActivityType.PROJECT_OWNERSHIP_TRANSFERRED,
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
