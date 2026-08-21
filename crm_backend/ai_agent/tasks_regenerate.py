"""Celery tasks for AI content regeneration -- both the comment-driven,
partial plan regeneration (still-draft AIGeneratedTask rows) and the
single-field regeneration of an already-saved Task's description.

Both call the same FastAPI endpoint (/api/v1/project-plan-regenerate) with
different amounts of context, and are kept in their own module rather than
merged into ai_agent/tasks.py, matching the existing convention in
tasks_assistant.py (small HTTP-call duplication in exchange for isolated,
easy-to-patch tests per feature).
"""

import logging
import uuid

import requests
from celery import shared_task
from departments_and_teams.models import Department
from django.conf import settings
from django.utils import timezone
from projects_and_tasks.models import TaskType

from .models import AIGeneratedTask, AIGeneration, AITaskContentRegeneration

logger = logging.getLogger(__name__)

DEFAULT_TASK_REGENERATION_INSTRUCTIONS = (
    'Improve and refine this task description for clarity and completeness, '
    'keeping the same scope and intent.'
)


class TransientAIServiceError(Exception):
    """Network error, timeout, or 5xx/429 from the AI service -- safe to retry."""


class PermanentAIRegenerationError(Exception):
    """The AI service rejected the request or returned invalid output."""


def _generated_task_ref(item: AIGeneratedTask) -> dict:
    return {
        'temporary_id': item.temporary_id,
        'sequence': item.sequence,
        'title': item.title,
        'description': item.description,
        'priority': item.priority,
        'estimated_effort': item.estimated_effort,
        'dependency_ids': item.dependency_temp_ids,
        'suggested_department_id': str(item.suggested_department_id) if item.suggested_department_id else None,
        'suggested_task_type_id': str(item.suggested_task_type_id) if item.suggested_task_type_id else None,
    }


def _call_ai_service(payload: dict) -> dict:
    headers = {}
    if settings.WORKROOM_AI_SERVICE_TOKEN:
        headers['X-Service-Token'] = settings.WORKROOM_AI_SERVICE_TOKEN
    try:
        response = requests.post(
            f'{settings.WORKROOM_AI_SERVICE_URL}/api/v1/project-plan-regenerate',
            json=payload, headers=headers, timeout=settings.WORKROOM_AI_SERVICE_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TransientAIServiceError(str(exc)) from exc

    if response.status_code == 200:
        return response.json()
    if response.status_code in (429, 503) or response.status_code >= 500:
        raise TransientAIServiceError(f'AI service returned {response.status_code}: {response.text[:500]}')
    raise PermanentAIRegenerationError(f'AI service rejected the request ({response.status_code}): {response.text[:500]}')


# --------------------------------------------------------------------------
# Partial plan regeneration (pre-save, draft AIGeneratedTask rows)
# --------------------------------------------------------------------------

@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_ai_plan_regeneration(self, generation_id: str):
    generation = AIGeneration.objects.filter(id=generation_id).select_related('project', 'project__company').first()
    if generation is None:
        logger.warning('ai_plan_regeneration.not_found', extra={'generation_id': str(generation_id)})
        return
    if generation.status != AIGeneration.STATUS.PROCESSING:
        # The router flips status to PROCESSING synchronously before
        # enqueueing -- if it's not PROCESSING, a duplicate delivery of this
        # same task already handled it (idempotency, Rule 8).
        logger.info('ai_plan_regeneration.skipped_terminal_status', extra={'generation_id': str(generation_id)})
        return

    commented = list(generation.generated_tasks.filter(comment_resolved=False))
    if not commented:
        logger.warning('ai_plan_regeneration.nothing_to_regenerate', extra={'generation_id': str(generation_id)})
        AIGeneration.objects.filter(id=generation.id).update(status=AIGeneration.STATUS.COMPLETED)
        return

    all_tasks = list(generation.generated_tasks.all())
    payload = {
        'generation_id': str(generation.id),
        'project_id': str(generation.project_id),
        'title': generation.project.title,
        'description': generation.project.description,
        'departments': [
            {'id': str(d.id), 'name': d.name} for d in Department.objects.filter(company=generation.project.company)
        ],
        'task_types': [
            {'id': str(t.id), 'name': t.name} for t in TaskType.objects.filter(company=generation.project.company)
        ],
        'existing_tasks': [_generated_task_ref(item) for item in all_tasks],
        'tasks_to_regenerate': [
            {
                'temporary_id': item.temporary_id, 'title': item.title, 'description': item.description,
                'reviewer_comment': item.reviewer_comment,
            }
            for item in commented
        ],
    }

    try:
        response_body = _call_ai_service(payload)
    except TransientAIServiceError as exc:
        logger.warning('ai_plan_regeneration.transient_failure', extra={'generation_id': str(generation.id), 'error': str(exc)})
        if self.request.retries >= self.max_retries:
            _revert_with_error(generation, f'AI service unavailable after {self.max_retries} retries: {exc}')
            return
        raise self.retry(exc=exc)
    except PermanentAIRegenerationError as exc:
        logger.error('ai_plan_regeneration.permanent_failure', extra={'generation_id': str(generation.id), 'error': str(exc)})
        _revert_with_error(generation, str(exc))
        return

    result_data = response_body.get('data') or {}
    try:
        _apply_regenerated_tasks(generation, commented, result_data.get('tasks') or [])
    except ValueError as exc:
        logger.error('ai_plan_regeneration.apply_failed', extra={'generation_id': str(generation.id), 'error': str(exc)})
        _revert_with_error(generation, str(exc))
        return

    generation.status = AIGeneration.STATUS.COMPLETED
    generation.error_message = ''
    generation.provider = (result_data.get('provider') or generation.provider)[:50]
    generation.model = (result_data.get('model') or generation.model)[:100]
    generation.save(update_fields=['status', 'error_message', 'provider', 'model'])
    logger.info('ai_plan_regeneration.completed', extra={'generation_id': str(generation.id), 'regenerated_count': len(commented)})


def _revert_with_error(generation: AIGeneration, error_message: str):
    """A failed regeneration must not destroy the already-drafted plan --
    only the initial generation's failure means 'no plan exists.' Revert to
    COMPLETED (the plan is still there, review/save still work) and surface
    the failure via error_message; commented rows stay comment_resolved=False
    so Regenerate Plan remains available to retry."""
    generation.status = AIGeneration.STATUS.COMPLETED
    generation.error_message = error_message[:2000]
    generation.save(update_fields=['status', 'error_message'])


def _apply_regenerated_tasks(generation: AIGeneration, commented: list, returned_tasks: list[dict]):
    by_temp_id = {item.temporary_id: item for item in commented}
    returned_by_temp_id = {item['temporary_id']: item for item in returned_tasks if item.get('temporary_id')}
    if set(returned_by_temp_id) != set(by_temp_id):
        raise ValueError('AI service returned a different set of tasks than was requested for regeneration.')

    company_department_ids = set(
        Department.objects.filter(company=generation.project.company).values_list('id', flat=True),
    )
    company_task_type_ids = set(
        TaskType.objects.filter(company=generation.project.company).values_list('id', flat=True),
    )

    to_update = []
    for temp_id, item in returned_by_temp_id.items():
        row = by_temp_id[temp_id]

        department_id = None
        raw_department_id = item.get('suggested_department_id')
        if raw_department_id:
            department_id = uuid.UUID(str(raw_department_id))
            if department_id not in company_department_ids:
                raise ValueError(f"Task '{temp_id}' suggested a department outside this company.")

        task_type_id = None
        raw_task_type_id = item.get('suggested_task_type_id')
        if raw_task_type_id:
            task_type_id = uuid.UUID(str(raw_task_type_id))
            if task_type_id not in company_task_type_ids:
                raise ValueError(f"Task '{temp_id}' suggested a task type outside this company.")

        priority = item.get('priority') or row.priority
        if priority not in AIGeneratedTask.PRIORITY.values:
            priority = row.priority

        row.title = (item.get('title') or row.title)[:255]
        row.description = item.get('description') or ''
        row.priority = priority
        row.estimated_effort = (item.get('estimated_effort') or '')[:100]
        row.suggested_department_id = department_id
        row.suggested_task_type_id = task_type_id
        row.comment_resolved = True
        to_update.append(row)

    AIGeneratedTask.objects.bulk_update(
        to_update,
        ['title', 'description', 'priority', 'estimated_effort', 'suggested_department', 'suggested_task_type', 'comment_resolved', 'updated_at'],
    )


# --------------------------------------------------------------------------
# Single saved-task content regeneration (post-save, real Task row)
# --------------------------------------------------------------------------

@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_task_content_regeneration(self, regeneration_id: str):
    regeneration = AITaskContentRegeneration.objects.filter(id=regeneration_id).select_related(
        'task', 'task__project', 'task__project__company',
    ).first()
    if regeneration is None:
        logger.warning('ai_task_content_regeneration.not_found', extra={'regeneration_id': str(regeneration_id)})
        return
    if regeneration.status not in (AITaskContentRegeneration.STATUS.PENDING, AITaskContentRegeneration.STATUS.PROCESSING):
        logger.info('ai_task_content_regeneration.skipped_terminal_status', extra={'regeneration_id': str(regeneration_id)})
        return

    AITaskContentRegeneration.objects.filter(id=regeneration.id).update(
        status=AITaskContentRegeneration.STATUS.PROCESSING, started_at=timezone.now(),
    )

    task = regeneration.task
    temp_id = str(task.id)
    payload = {
        'generation_id': str(uuid.uuid4()),  # no owning AIGeneration for a post-save regeneration; FastAPI only logs this id
        'project_id': str(task.project_id),
        'title': task.project.title,
        'description': task.project.description,
        'existing_tasks': [],
        'tasks_to_regenerate': [{
            'temporary_id': temp_id, 'title': task.title, 'description': task.description,
            'reviewer_comment': regeneration.instructions or DEFAULT_TASK_REGENERATION_INSTRUCTIONS,
        }],
    }

    try:
        response_body = _call_ai_service(payload)
    except TransientAIServiceError as exc:
        logger.warning('ai_task_content_regeneration.transient_failure', extra={'regeneration_id': str(regeneration.id), 'error': str(exc)})
        if self.request.retries >= self.max_retries:
            _mark_task_regeneration_failed(regeneration, f'AI service unavailable after {self.max_retries} retries: {exc}')
            return
        raise self.retry(exc=exc)
    except PermanentAIRegenerationError as exc:
        logger.error('ai_task_content_regeneration.permanent_failure', extra={'regeneration_id': str(regeneration.id), 'error': str(exc)})
        _mark_task_regeneration_failed(regeneration, str(exc))
        return

    result_data = response_body.get('data') or {}
    returned = next((item for item in (result_data.get('tasks') or []) if item.get('temporary_id') == temp_id), None)
    new_description = (returned or {}).get('description', '').strip()
    if not returned or not new_description:
        _mark_task_regeneration_failed(regeneration, 'AI service did not return a revised description.')
        return

    regeneration.previous_description = task.description
    regeneration.status = AITaskContentRegeneration.STATUS.COMPLETED
    regeneration.completed_at = timezone.now()
    regeneration.provider = (result_data.get('provider') or '')[:50]
    regeneration.model = (result_data.get('model') or '')[:100]
    regeneration.save(update_fields=[
        'previous_description', 'status', 'completed_at', 'provider', 'model',
    ])

    # Content-only: never touches created_by/assigned_to/project/source/sequence
    # -- those fields are simply never part of this write.
    task.description = new_description
    task.save(update_fields=['description', 'updated_at'])
    logger.info('ai_task_content_regeneration.completed', extra={'regeneration_id': str(regeneration.id), 'task_id': str(task.id)})


def _mark_task_regeneration_failed(regeneration: AITaskContentRegeneration, error_message: str):
    regeneration.status = AITaskContentRegeneration.STATUS.FAILED
    regeneration.completed_at = timezone.now()
    regeneration.error_message = error_message[:2000]
    regeneration.save(update_fields=['status', 'completed_at', 'error_message'])
