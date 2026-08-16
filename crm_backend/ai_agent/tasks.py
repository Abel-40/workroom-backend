"""Celery tasks for AI project decomposition.

process_ai_generation: marks PROCESSING, calls the FastAPI AI service,
independently re-validates the structured plan against this company's real
department/task-type records (never trust the AI service's own validation
alone -- Rule 9), and persists it transactionally via
projects_and_tasks.services (Phase 8). Retries transient failures; a
permanently invalid or rejected plan fails the generation outright rather
than retrying forever ("Do not endlessly retry invalid AI output").
"""

import logging

import requests
from celery import shared_task
from departments_and_teams.models import Department
from django.conf import settings
from django.utils import timezone
from notifications_and_activity.services import notify_ai_generation_completed, notify_ai_generation_failed
from projects_and_tasks.models import TaskType
from projects_and_tasks.services import persist_ai_generated_tasks

from .models import AIGeneration

logger = logging.getLogger(__name__)


class TransientAIServiceError(Exception):
    """Network error, timeout, or 5xx/429 from the AI service -- safe to retry."""


class PermanentAIGenerationError(Exception):
    """The AI service rejected the request or returned invalid output."""


def _build_request_payload(generation: AIGeneration) -> dict:
    project = generation.project
    departments = Department.objects.filter(company=project.company).values('id', 'name')
    task_types = TaskType.objects.filter(company=project.company).values('id', 'name')
    return {
        'generation_id': str(generation.id),
        'project_id': str(project.id),
        'title': project.title,
        'description': project.description,
        'requirements': '',
        'departments': [{'id': str(d['id']), 'name': d['name']} for d in departments],
        'task_types': [{'id': str(t['id']), 'name': t['name']} for t in task_types],
    }


def _call_ai_service(payload: dict) -> dict:
    headers = {}
    if settings.WORKROOM_AI_SERVICE_TOKEN:
        headers['X-Service-Token'] = settings.WORKROOM_AI_SERVICE_TOKEN
    try:
        response = requests.post(
            f'{settings.WORKROOM_AI_SERVICE_URL}/api/v1/project-plan',
            json=payload, headers=headers, timeout=settings.WORKROOM_AI_SERVICE_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TransientAIServiceError(str(exc)) from exc

    if response.status_code == 200:
        return response.json()
    if response.status_code in (429, 503) or response.status_code >= 500:
        raise TransientAIServiceError(f'AI service returned {response.status_code}: {response.text[:500]}')
    raise PermanentAIGenerationError(f'AI service rejected the request ({response.status_code}): {response.text[:500]}')


def _mark_failed(generation: AIGeneration, error_message: str):
    generation.status = AIGeneration.STATUS.FAILED
    generation.completed_at = timezone.now()
    generation.error_message = error_message[:2000]
    generation.save(update_fields=['status', 'completed_at', 'error_message'])
    notify_ai_generation_failed(generation)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_ai_generation(self, generation_id: str):
    generation = AIGeneration.objects.filter(id=generation_id).select_related('project', 'project__company').first()
    if generation is None:
        logger.warning('ai_generation.not_found', extra={'generation_id': str(generation_id)})
        return
    if generation.status not in (AIGeneration.STATUS.PENDING, AIGeneration.STATUS.PROCESSING):
        # Already finished by an earlier delivery of this same task -- retries
        # must be idempotent (Rule 8), so re-processing here would be wrong.
        logger.info('ai_generation.skipped_terminal_status', extra={'generation_id': str(generation_id)})
        return

    AIGeneration.objects.filter(id=generation.id).update(
        status=AIGeneration.STATUS.PROCESSING, started_at=timezone.now(),
    )

    payload = _build_request_payload(generation)
    try:
        response_body = _call_ai_service(payload)
    except TransientAIServiceError as exc:
        logger.warning('ai_generation.transient_failure', extra={'generation_id': str(generation.id), 'error': str(exc)})
        if self.request.retries >= self.max_retries:
            _mark_failed(generation, f'AI service unavailable after {self.max_retries} retries: {exc}')
            return
        raise self.retry(exc=exc)
    except PermanentAIGenerationError as exc:
        logger.error('ai_generation.permanent_failure', extra={'generation_id': str(generation.id), 'error': str(exc)})
        _mark_failed(generation, str(exc))
        return

    plan_data = response_body.get('data') or {}
    try:
        created_count = persist_ai_generated_tasks(generation, plan_data)
    except ValueError as exc:
        logger.error('ai_generation.persist_failed', extra={'generation_id': str(generation.id), 'error': str(exc)})
        _mark_failed(generation, str(exc))
        return

    generation.status = AIGeneration.STATUS.COMPLETED
    generation.completed_at = timezone.now()
    generation.task_count = created_count
    generation.provider = plan_data.get('provider', '')[:50]
    generation.model = plan_data.get('model', '')[:100]
    generation.save(update_fields=['status', 'completed_at', 'task_count', 'provider', 'model'])
    notify_ai_generation_completed(generation)
    logger.info('ai_generation.completed', extra={'generation_id': str(generation.id), 'task_count': created_count})
