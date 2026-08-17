"""Celery task for the AI project health summary.

process_health_summary: marks PROCESSING, computes real project stats
(analytics/services.py::get_project_stats), calls the FastAPI AI service,
and persists the result. Never claims anything not derivable from those
stats -- see the system prompt in workroom-ai/apps/services/health_services.py.

Deliberately not merged into ai_agent/tasks.py, same reasoning as
tasks_assistant.py (keeps that module's existing requests.post patch targets
intact).
"""

import logging

import requests
from analytics.services import get_project_stats
from asgiref.sync import async_to_sync
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import AIProjectHealthSummary

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 2000


class TransientAIServiceError(Exception):
    """Network error, timeout, or 5xx/429 from the AI service -- safe to retry."""


class PermanentHealthSummaryError(Exception):
    """The AI service rejected the request or returned invalid output."""


def _build_request_payload(summary: AIProjectHealthSummary) -> dict:
    project = summary.project
    stats = async_to_sync(get_project_stats)(project)
    return {
        'summary_id': str(summary.id),
        'project_id': str(project.id),
        'project_title': project.title,
        'project_description': project.description,
        'stats': stats,
    }


def _call_ai_service(payload: dict) -> dict:
    headers = {}
    if settings.WORKROOM_AI_SERVICE_TOKEN:
        headers['X-Service-Token'] = settings.WORKROOM_AI_SERVICE_TOKEN
    try:
        response = requests.post(
            f'{settings.WORKROOM_AI_SERVICE_URL}/api/v1/project-health-summary',
            json=payload, headers=headers, timeout=settings.WORKROOM_AI_SERVICE_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TransientAIServiceError(str(exc)) from exc

    if response.status_code == 200:
        return response.json()
    if response.status_code in (429, 503) or response.status_code >= 500:
        raise TransientAIServiceError(f'AI service returned {response.status_code}: {response.text[:500]}')
    raise PermanentHealthSummaryError(
        f'AI service rejected the request ({response.status_code}): {response.text[:500]}',
    )


def _mark_failed(summary: AIProjectHealthSummary, error_message: str):
    summary.status = AIProjectHealthSummary.STATUS.FAILED
    summary.completed_at = timezone.now()
    summary.error_message = error_message[:2000]
    summary.save(update_fields=['status', 'completed_at', 'error_message'])


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_health_summary(self, summary_id: str):
    summary = AIProjectHealthSummary.objects.filter(id=summary_id).select_related(
        'project', 'project__company',
    ).first()
    if summary is None:
        logger.warning('ai_health_summary.not_found', extra={'summary_id': str(summary_id)})
        return
    if summary.status not in (AIProjectHealthSummary.STATUS.PENDING, AIProjectHealthSummary.STATUS.PROCESSING):
        logger.info('ai_health_summary.skipped_terminal_status', extra={'summary_id': str(summary_id)})
        return

    AIProjectHealthSummary.objects.filter(id=summary.id).update(
        status=AIProjectHealthSummary.STATUS.PROCESSING, started_at=timezone.now(),
    )

    payload = _build_request_payload(summary)
    try:
        response_body = _call_ai_service(payload)
    except TransientAIServiceError as exc:
        logger.warning('ai_health_summary.transient_failure', extra={'summary_id': str(summary.id), 'error': str(exc)})
        if self.request.retries >= self.max_retries:
            _mark_failed(summary, f'AI service unavailable after {self.max_retries} retries: {exc}')
            return
        raise self.retry(exc=exc)
    except PermanentHealthSummaryError as exc:
        logger.error('ai_health_summary.permanent_failure', extra={'summary_id': str(summary.id), 'error': str(exc)})
        _mark_failed(summary, str(exc))
        return

    data = response_body.get('data') or {}
    summary_text = (data.get('summary') or '').strip()
    risk_level = data.get('risk_level') or ''
    # Defensive re-check even though FastAPI's Pydantic Literal should
    # already reject a bad value -- never trust the AI service's own
    # validation alone.
    if risk_level not in AIProjectHealthSummary.RISK_LEVEL.values:
        risk_level = ''
    if not summary_text:
        _mark_failed(summary, 'AI service returned an empty summary.')
        return

    summary.status = AIProjectHealthSummary.STATUS.COMPLETED
    summary.completed_at = timezone.now()
    summary.summary = summary_text[:MAX_SUMMARY_CHARS]
    summary.risk_level = risk_level
    summary.provider = (data.get('provider') or '')[:50]
    summary.model = (data.get('model') or '')[:100]
    summary.save(update_fields=['status', 'completed_at', 'summary', 'risk_level', 'provider', 'model'])
    logger.info('ai_health_summary.completed', extra={'summary_id': str(summary.id), 'risk_level': risk_level})
