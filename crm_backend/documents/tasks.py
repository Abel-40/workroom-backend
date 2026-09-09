"""Background jobs for documents."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def purge_expired_documents_task():
    """Celery Beat entry: permanently remove documents whose retention window
    has passed. See documents.services.purge_expired_documents for why the
    files are removed one at a time rather than by a bulk queryset delete."""
    from .services import purge_expired_documents

    purged = purge_expired_documents()
    if purged:
        logger.info('documents.purged count=%d', purged)
    return purged
