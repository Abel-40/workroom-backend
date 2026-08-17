"""Project health summary lifecycle: create a traceable PENDING record and
hand off to Celery. Never talks to the FastAPI AI service directly -- see
ai_agent/tasks_health.py. Kept separate from ai_agent/services.py, which is
scoped to the AI-decomposition flow only.
"""

import logging

from asgiref.sync import sync_to_async
from projects_and_tasks.services import user_can_view_project

from .models import AIProjectHealthSummary
from .tasks_health import process_health_summary

logger = logging.getLogger(__name__)


async def request_health_summary(user, project):
    if not await user_can_view_project(user, project):
        return None, 'forbidden'
    summary = await AIProjectHealthSummary.objects.acreate(project=project, requested_by=user)
    try:
        await sync_to_async(process_health_summary.delay, thread_sensitive=True)(str(summary.id))
    except Exception:
        summary.status = AIProjectHealthSummary.STATUS.FAILED
        summary.error_message = 'Failed to queue the health summary job.'
        await summary.asave(update_fields=['status', 'error_message'])
        logger.exception('ai_health_summary.enqueue_failed', extra={'summary_id': str(summary.id)})
        return summary, 'queue_failed'
    return summary, None


async def get_health_summary_for_user(user, summary_id):
    summary = await AIProjectHealthSummary.objects.select_related('project', 'project__company').filter(
        id=summary_id,
    ).afirst()
    if summary is None:
        return None, 'not_found'
    if not await user_can_view_project(user, summary.project):
        return None, 'forbidden'
    return summary, None
