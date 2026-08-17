"""Invite-email retry sweep, shared by the Celery Beat periodic task
(users/tasks.py) and the send_pending_invites management command so there is
one implementation instead of two.
"""

import logging

from django.conf import settings
from utils.Invitation_email import send_invitation_email
from utils.tokens import generate_token, hash_token

from .models import PendingInvite

logger = logging.getLogger(__name__)


def retry_pending_invite_emails() -> dict:
    """Retry invitation emails for invites where email_sent is still False.

    Only a hash of each invite's token is ever stored (utils/tokens.py), so
    the original email/link can't be reconstructed for a resend -- this
    issues a fresh token instead, which also invalidates whatever token may
    have partially leaked (e.g. into mail server logs) from the failed try.
    """
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://your-frontend.com').rstrip('/')
    invites = PendingInvite.objects.filter(
        status=PendingInvite.Status.Pending, email_sent=False,
    ).select_related('company')

    sent = expired = failed = 0
    for invite in invites:
        if invite.is_expired():
            invite.status = PendingInvite.Status.Expired
            invite.save(update_fields=['status'])
            expired += 1
            continue

        raw_token = generate_token()
        invite.token_hash = hash_token(raw_token)
        try:
            send_invitation_email(
                invite.email, f'The {invite.company.name} team', invite.company.name,
                f'{frontend_url}/invite/accept?token={raw_token}',
            )
        except Exception:
            invite.save(update_fields=['token_hash'])
            failed += 1
            logger.warning('invite_email.retry_failed', extra={'invite_id': str(invite.id)})
            continue

        invite.email_sent = True
        invite.save(update_fields=['token_hash', 'email_sent'])
        sent += 1

    result = {'sent': sent, 'expired': expired, 'failed': failed}
    logger.info('invite_email.retry_sweep_completed', extra=result)
    return result
