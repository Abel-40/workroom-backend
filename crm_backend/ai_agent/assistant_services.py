"""Project assistant query lifecycle: create a traceable PENDING record and
hand off to Celery. Never talks to the FastAPI AI service directly -- see
ai_agent/tasks_assistant.py. Kept separate from ai_agent/services.py, which
is scoped to the AI-decomposition flow only.
"""

import logging

from asgiref.sync import sync_to_async
from pages.services import get_pages_by_ids_for_company
from projects_and_tasks.services import user_can_manage_project, user_can_view_project

from .models import AIAssistantQuery
from .tasks_assistant import process_assistant_query

logger = logging.getLogger(__name__)


async def request_assistant_query(user, project, question: str, reference_url: str | None, page_ids: list | None = None):
    if not await user_can_view_project(user, project):
        return None, 'forbidden'
    page_ids = page_ids or []
    pages = await get_pages_by_ids_for_company(project.company, page_ids)
    if len(pages) != len(set(page_ids)):
        # Fail closed (Rule 4): a page id that doesn't resolve to this
        # company is rejected outright, not silently dropped.
        return None, 'invalid_page'
    query = await AIAssistantQuery.objects.acreate(
        project=project, requested_by=user, question=question, reference_url=reference_url or '',
    )
    if pages:
        await query.pages.aset(pages)
    try:
        # thread_sensitive=True: under CELERY_TASK_ALWAYS_EAGER (tests),
        # .delay() runs inline on this same thread/connection, which must
        # see the just-created, not-yet-committed query row.
        await sync_to_async(process_assistant_query.delay, thread_sensitive=True)(str(query.id))
    except Exception:
        query.status = AIAssistantQuery.STATUS.FAILED
        query.error_message = 'Failed to queue the assistant query job.'
        await query.asave(update_fields=['status', 'error_message'])
        logger.exception('ai_assistant_query.enqueue_failed', extra={'query_id': str(query.id)})
        return query, 'queue_failed'
    return query, None


async def get_assistant_query_for_user(user, query_id):
    query = await AIAssistantQuery.objects.select_related('project', 'project__company').filter(
        id=query_id,
    ).afirst()
    if query is None:
        return None, 'not_found'
    if not await user_can_view_project(user, query.project):
        return None, 'forbidden'
    return query, None


async def delete_assistant_query(user, query) -> str | None:
    """Remove a query from the requester's own history. Restricted to the
    requester or someone who can manage the project -- viewing is broader
    (any project member) but deleting another member's question isn't."""
    if not (query.requested_by_id == user.id or await user_can_manage_project(user, query.project)):
        return 'forbidden'
    await query.adelete()
    return None
