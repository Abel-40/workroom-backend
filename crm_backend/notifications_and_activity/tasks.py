"""Celery task for notification email delivery.

Follows the same idempotency shape as ai_agent.tasks.process_ai_generation:
re-fetch the row and bail if it's already been delivered (or is gone) rather
than trusting this is the first delivery attempt -- a task can run twice
(Rule 8). No Celery Beat retry sweep here, unlike invite emails: the in-app
Notification row already exists regardless of email delivery outcome, so a
notification email is supplementary, not irreplaceable -- this task's own
retry window is enough, and a permanent failure simply doesn't retry further
(same "don't endlessly retry" posture as AI generation).
"""

import logging

from celery import shared_task
from utils.notification_email import send_notification_email

from .models import Notification

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_notification_email_task(self, notification_id: str):
    notification = Notification.objects.filter(
        id=notification_id, email_sent=False,
    ).select_related('recipient').first()
    if notification is None:
        # Already sent (or the row is gone) -- idempotent no-op.
        logger.info('notification_email.skipped_already_sent', extra={'notification_id': notification_id})
        return

    try:
        send_notification_email(
            notification.recipient.email, notification.title, notification.message, notification.type,
        )
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                'notification_email.permanent_failure',
                extra={'notification_id': notification_id, 'error': str(exc)},
            )
            return
        raise self.retry(exc=exc)

    notification.email_sent = True
    notification.save(update_fields=['email_sent'])
    logger.info('notification_email.sent', extra={'notification_id': notification_id})
