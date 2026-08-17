"""Celery task for the scoped project assistant.

process_assistant_query: marks PROCESSING, optionally fetches a user-supplied
reference URL (SSRF-safe, utils/safe_fetch.py) and the project's own text
documents, calls the FastAPI AI service, and persists the answer -- parsing
the OUT_OF_SCOPE: refusal sentinel here (not in the FastAPI service), so
that parsing lives in exactly one place. Retries transient failures; a
permanent rejection fails the query outright.

Deliberately not merged into ai_agent/tasks.py: that module's existing tests
patch 'ai_agent.tasks.requests.post' directly, and gemini.py/deepseek.py
already duplicate this same HTTP-call shape rather than sharing it -- a
small amount of duplication here keeps those patch targets intact.
"""

import logging

import requests
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from projects_and_tasks.models import Task
from projects_and_tasks.services import get_text_document_excerpts
from utils import safe_fetch

from .models import AIAssistantQuery

logger = logging.getLogger(__name__)

OUT_OF_SCOPE_PREFIX = 'OUT_OF_SCOPE:'
MAX_TASK_TITLES = 30
MAX_ANSWER_CHARS = 5000


class TransientAIServiceError(Exception):
    """Network error, timeout, or 5xx/429 from the AI service -- safe to retry."""


class PermanentAssistantError(Exception):
    """The AI service rejected the request or returned invalid output."""


def _build_request_payload(query: AIAssistantQuery, reference_url_content: str) -> dict:
    project = query.project
    task_titles = list(
        Task.objects.filter(project=project, is_deleted=False)
        .order_by('-created_at').values_list('title', flat=True)[:MAX_TASK_TITLES],
    )
    document_excerpts = get_text_document_excerpts(project)
    return {
        'query_id': str(query.id),
        'project_id': str(project.id),
        'question': query.question,
        'project_title': project.title,
        'project_description': project.description,
        'task_titles': task_titles,
        'reference_url': query.reference_url or None,
        'reference_url_content': reference_url_content,
        'document_excerpts': document_excerpts,
    }


def _call_ai_service(payload: dict) -> dict:
    headers = {}
    if settings.WORKROOM_AI_SERVICE_TOKEN:
        headers['X-Service-Token'] = settings.WORKROOM_AI_SERVICE_TOKEN
    try:
        response = requests.post(
            f'{settings.WORKROOM_AI_SERVICE_URL}/api/v1/assistant',
            json=payload, headers=headers, timeout=settings.WORKROOM_AI_SERVICE_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TransientAIServiceError(str(exc)) from exc

    if response.status_code == 200:
        return response.json()
    if response.status_code in (429, 503) or response.status_code >= 500:
        raise TransientAIServiceError(f'AI service returned {response.status_code}: {response.text[:500]}')
    raise PermanentAssistantError(f'AI service rejected the request ({response.status_code}): {response.text[:500]}')


def _mark_failed(query: AIAssistantQuery, error_message: str):
    query.status = AIAssistantQuery.STATUS.FAILED
    query.completed_at = timezone.now()
    query.error_message = error_message[:2000]
    query.save(update_fields=['status', 'completed_at', 'error_message'])


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_assistant_query(self, query_id: str):
    query = AIAssistantQuery.objects.filter(id=query_id).select_related('project', 'project__company').first()
    if query is None:
        logger.warning('ai_assistant_query.not_found', extra={'query_id': str(query_id)})
        return
    if query.status not in (AIAssistantQuery.STATUS.PENDING, AIAssistantQuery.STATUS.PROCESSING):
        logger.info('ai_assistant_query.skipped_terminal_status', extra={'query_id': str(query_id)})
        return

    AIAssistantQuery.objects.filter(id=query.id).update(
        status=AIAssistantQuery.STATUS.PROCESSING, started_at=timezone.now(),
    )

    reference_url_content = ''
    if query.reference_url:
        try:
            reference_url_content = safe_fetch.fetch_text(query.reference_url)
        except safe_fetch.UnsafeURLError as exc:
            # Hard failure: never silently proceed as if the URL had been
            # safely read -- that would look successful when it wasn't.
            logger.warning('ai_assistant_query.unsafe_url', extra={'query_id': str(query.id), 'error': str(exc)})
            _mark_failed(query, 'The provided URL could not be safely accessed.')
            return
        except safe_fetch.FetchFailedError as exc:
            # Ordinary fetch failure: degrade gracefully, answer from
            # project context alone.
            logger.warning('ai_assistant_query.fetch_failed', extra={'query_id': str(query.id), 'error': str(exc)})

    payload = _build_request_payload(query, reference_url_content)
    try:
        response_body = _call_ai_service(payload)
    except TransientAIServiceError as exc:
        logger.warning('ai_assistant_query.transient_failure', extra={'query_id': str(query.id), 'error': str(exc)})
        if self.request.retries >= self.max_retries:
            _mark_failed(query, f'AI service unavailable after {self.max_retries} retries: {exc}')
            return
        raise self.retry(exc=exc)
    except PermanentAssistantError as exc:
        logger.error('ai_assistant_query.permanent_failure', extra={'query_id': str(query.id), 'error': str(exc)})
        _mark_failed(query, str(exc))
        return

    data = response_body.get('data') or {}
    answer_text = (data.get('answer') or '').strip()
    if not answer_text:
        _mark_failed(query, 'AI service returned an empty answer.')
        return

    refused = answer_text.startswith(OUT_OF_SCOPE_PREFIX)
    if refused:
        answer_text = answer_text[len(OUT_OF_SCOPE_PREFIX):].strip()

    query.status = AIAssistantQuery.STATUS.COMPLETED
    query.completed_at = timezone.now()
    query.answer = answer_text[:MAX_ANSWER_CHARS]
    query.refused = refused
    query.provider = (data.get('provider') or '')[:50]
    query.model = (data.get('model') or '')[:100]
    query.save(update_fields=['status', 'completed_at', 'answer', 'refused', 'provider', 'model'])
    logger.info('ai_assistant_query.completed', extra={'query_id': str(query.id), 'refused': refused})
