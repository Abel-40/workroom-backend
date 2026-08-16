"""Notification creation helpers.

Plain sync Django ORM calls: called directly from sync contexts (Celery
tasks, accept_invite_in_transaction) and via sync_to_async from async
contexts (projects_and_tasks.services), matching the sync_to_async
convention already used throughout this codebase for the same reason.
"""

from .models import Notification


def _create(recipient, type_, title, message='', related_object_type='', related_object_id=None):
    if recipient is None:
        return None
    return Notification.objects.create(
        recipient=recipient, type=type_, title=title, message=message,
        related_object_type=related_object_type, related_object_id=related_object_id,
    )


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
    _create(
        generation.requested_by, Notification.Type.AI_GENERATION_FAILED,
        f"AI plan failed for '{generation.project.title}'",
        message=(generation.error_message or '')[:500],
        related_object_type='ai_generation', related_object_id=generation.id,
    )
