"""Celery tasks for AI project decomposition.

Phase 5 scope only: prove the queueing plumbing works and record that a
generation was picked up. Calling the FastAPI AI service, validating its
structured output, and persisting generated tasks is Phase 7's job -- this
task intentionally stops short of that so it isn't presented as a finished
AI feature (DEVELOPMENT_RULES Rule 14).
"""

import logging

from celery import shared_task
from django.utils import timezone

from .models import AIGeneration

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_ai_generation(self, generation_id: str):
    """Mark a generation PROCESSING. Retry-safe: only ever transitions a
    PENDING generation, so re-delivery of the same task is a no-op."""
    updated = AIGeneration.objects.filter(id=generation_id, status=AIGeneration.STATUS.PENDING).update(
        status=AIGeneration.STATUS.PROCESSING, started_at=timezone.now(),
    )
    if updated:
        logger.info('ai_generation.picked_up', extra={'generation_id': str(generation_id)})
    else:
        logger.info('ai_generation.skipped_not_pending', extra={'generation_id': str(generation_id)})
