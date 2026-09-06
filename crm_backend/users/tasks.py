"""Celery tasks for invite-email delivery (Phase 9/11: keep email off the
request thread; see api/api.py::send_invite). Runs on the 'simple' queue
(CELERY_TASK_DEFAULT_QUEUE) -- fast, no external LLM calls.
"""

import logging

from celery import shared_task
from django.conf import settings
from utils.Invitation_email import send_invitation_email
from utils.welcome_email import send_welcome_email

from .models import PendingInvite, User
from .services import retry_pending_invite_emails

logger = logging.getLogger(__name__)


@shared_task
def send_invite_email_task(invite_id: str, raw_token: str, inviter_name: str):
    """Fire-and-forget send for a freshly created invite. On failure this
    simply leaves email_sent=False -- retry_pending_invite_emails_task
    (Celery Beat) is the single retry mechanism, matching the
    send_pending_invites management command used for manual/ops retries.
    """
    invite = PendingInvite.objects.filter(
        id=invite_id, status=PendingInvite.Status.Pending, email_sent=False,
    ).select_related('company').first()
    if invite is None:
        return
    if invite.is_expired():
        invite.delete()
        return
    frontend_url = settings.FRONTEND_URL
    try:
        send_invitation_email(
            invite.email, inviter_name, invite.company.name,
            f'{frontend_url}/invite/accept?token={raw_token}',
        )
    except Exception:
        logger.exception('invite_email.send_failed', extra={'invite_id': invite_id})
        return
    invite.email_sent = True
    invite.save(update_fields=['email_sent'])


@shared_task
def retry_pending_invite_emails_task():
    """Celery Beat schedule entry (see settings.CELERY_BEAT_SCHEDULE)."""
    return retry_pending_invite_emails()


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_welcome_email_task(self, user_id: str):
    """Same idempotency/retry shape as
    notifications_and_activity.tasks.send_notification_email_task: re-fetch
    filtered on the not-yet-sent flag rather than trusting this is the first
    delivery attempt (a task can run twice), and give up permanently after
    max_retries -- a welcome email is not irreplaceable."""
    user = User.objects.filter(id=user_id, welcome_email_sent=False).first()
    if user is None:
        logger.info('welcome_email.skipped_already_sent', extra={'user_id': user_id})
        return

    try:
        send_welcome_email(user.email, user.username)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error('welcome_email.permanent_failure', extra={'user_id': user_id, 'error': str(exc)})
            return
        raise self.retry(exc=exc)

    user.welcome_email_sent = True
    user.save(update_fields=['welcome_email_sent'])
    logger.info('welcome_email.sent', extra={'user_id': user_id})
