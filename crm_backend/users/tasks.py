"""Celery tasks for invite-email delivery (Phase 9/11: keep email off the
request thread; see api/api.py::send_invite). Runs on the 'simple' queue
(CELERY_TASK_DEFAULT_QUEUE) -- fast, no external LLM calls.
"""

import logging

from celery import shared_task
from django.conf import settings
from utils.Invitation_email import send_invitation_email

from .models import PendingInvite
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
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://your-frontend.com').rstrip('/')
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
