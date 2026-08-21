"""Celery tasks for AI project decomposition.

process_ai_generation: marks PROCESSING, calls the FastAPI AI service,
independently re-validates the structured plan against this company's real
department/task-type records (never trust the AI service's own validation
alone -- Rule 9), and stores it as draft AIGeneratedTask rows for human
review -- it does NOT create real Task rows; that only happens once a
reviewer explicitly saves the plan (projects_and_tasks.services.persist_ai_generated_tasks,
called from the /ai/generations/{id}/save/ endpoint). Retries transient
failures; a permanently invalid or rejected plan fails the generation
outright rather than retrying forever ("Do not endlessly retry invalid AI
output").
"""

import logging
import uuid

import requests
from celery import shared_task
from departments_and_teams.models import Department
from django.conf import settings
from django.utils import timezone
from notifications_and_activity.services import notify_ai_generation_completed, notify_ai_generation_failed
from projects_and_tasks.models import TaskType

from .models import AIGeneratedTask, AIGeneration

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
        'requirements': generation.prompt,
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
        created_count = _store_generated_tasks_for_review(generation, plan_data)
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


def _store_generated_tasks_for_review(generation: AIGeneration, plan_data: dict) -> int:
    """Validate the AI's plan against this company's real department/task-type
    records and store it as draft AIGeneratedTask rows for human review --
    deliberately does NOT create real Task rows (see module docstring).
    Django re-validates independently of the AI service's own validation
    (Rule 9); a department/task type/dependency reference outside what was
    actually supplied to the AI fails the whole generation.
    """
    project = generation.project
    tasks_data = plan_data.get('tasks') or []
    if not tasks_data:
        raise ValueError('AI service returned an empty task list.')

    company_department_ids = set(Department.objects.filter(company=project.company).values_list('id', flat=True))
    company_task_type_ids = set(TaskType.objects.filter(company=project.company).values_list('id', flat=True))

    known_temp_ids = set()
    for item in tasks_data:
        if not item.get('temporary_id') or not item.get('title'):
            raise ValueError('AI-generated task is missing temporary_id or title.')
        known_temp_ids.add(item['temporary_id'])

    to_create = []
    for item in tasks_data:
        dependency_ids = item.get('dependency_ids') or []
        for dep in dependency_ids:
            if dep not in known_temp_ids:
                raise ValueError(f"Task '{item['temporary_id']}' references an unknown dependency id.")

        department_id = None
        raw_department_id = item.get('suggested_department_id')
        if raw_department_id:
            department_id = uuid.UUID(str(raw_department_id))
            if department_id not in company_department_ids:
                raise ValueError(f"Task '{item['temporary_id']}' suggested a department outside this company.")

        task_type_id = None
        raw_task_type_id = item.get('suggested_task_type_id')
        if raw_task_type_id:
            task_type_id = uuid.UUID(str(raw_task_type_id))
            if task_type_id not in company_task_type_ids:
                raise ValueError(f"Task '{item['temporary_id']}' suggested a task type outside this company.")

        priority = item.get('priority') or AIGeneratedTask.PRIORITY.MEDIUM
        if priority not in AIGeneratedTask.PRIORITY.values:
            priority = AIGeneratedTask.PRIORITY.MEDIUM

        to_create.append(AIGeneratedTask(
            generation=generation, temporary_id=item['temporary_id'], sequence=item.get('sequence') or 0,
            title=item['title'][:255], description=item.get('description') or '',
            priority=priority, estimated_effort=(item.get('estimated_effort') or '')[:100],
            dependency_temp_ids=dependency_ids,
            suggested_department_id=department_id, suggested_task_type_id=task_type_id,
        ))

    AIGeneratedTask.objects.bulk_create(to_create)
    return len(to_create)
