"""AI generation lifecycle (Phase 5): create a traceable PENDING record and
hand off to Celery. This module never talks to an LLM or the FastAPI AI
service directly -- that boundary is Phase 6/7 (ARCHITECTURE.md Section 4/7).
"""

import logging

from asgiref.sync import sync_to_async
from projects_and_tasks.services import user_can_view_project

from .models import AIGeneration
from .tasks import process_ai_generation

logger = logging.getLogger(__name__)


async def request_project_plan(user, project):
    if not await user_can_view_project(user, project):
        return None, 'forbidden'
    generation = await AIGeneration.objects.acreate(project=project, requested_by=user)
    try:
        # thread_sensitive=True: under CELERY_TASK_ALWAYS_EAGER (tests),
        # .delay() runs the task body inline on whatever thread this runs
        # on -- it must be the same thread/connection as the just-created,
        # not-yet-committed generation row, or the task's own query for it
        # returns nothing. See api/api.py::send_invite for the same fix.
        await sync_to_async(process_ai_generation.delay, thread_sensitive=True)(str(generation.id))
    except Exception:
        generation.status = AIGeneration.STATUS.FAILED
        generation.error_message = 'Failed to queue the AI generation job.'
        await generation.asave(update_fields=['status', 'error_message'])
        logger.exception('ai_generation.enqueue_failed', extra={'generation_id': str(generation.id)})
        return generation, 'queue_failed'
    return generation, None


async def get_generation_for_user(user, generation_id):
    generation = await AIGeneration.objects.select_related('project', 'project__company').filter(
        id=generation_id,
    ).afirst()
    if generation is None:
        return None, 'not_found'
    if not await user_can_view_project(user, generation.project):
        return None, 'forbidden'
    return generation, None
