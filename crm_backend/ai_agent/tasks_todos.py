"""Celery task for personal to-do generation.

process_todo_generation: marks PROCESSING, calls the FastAPI AI service, then
**independently re-validates** the returned checklist before writing a single
row -- never trusting the AI service's own validation (Rule 9). Three things
are re-checked here that the AI service structurally cannot check:

1. every returned task id was one Django actually sent, and
2. that task is *still* assigned to the requester right now (it can be
   reassigned while the generation is in flight), and
3. every due date still falls inside the window Django computed in the
   requester's timezone.

Anything that fails is dropped; if nothing survives, the generation fails
rather than reporting success over an empty list.

Todos are persisted directly rather than staged for review: they are private,
individually deletable, and the whole batch can be dismissed in one action
(``ai_generation`` on TodoItem). That is still Generate -> Validate ->
Persist, just with the review step moved after the write instead of before
it -- which is the right trade for a checklist, unlike a project plan that
creates shared, assignable work.

Deliberately its own module rather than merged into ai_agent/tasks.py, same
reasoning as tasks_health.py and tasks_assistant.py.
"""

import logging

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from notifications_and_activity.services import notify_todos_generated, notify_todos_generation_failed
from projects_and_tasks.models import Task
from todos.models import TodoItem

from .models import AITodoGeneration

logger = logging.getLogger(__name__)


class TransientAIServiceError(Exception):
    """Network error, timeout, or 5xx/429 from the AI service -- safe to retry."""


class PermanentTodoGenerationError(Exception):
    """The AI service rejected the request or returned invalid output."""


def _build_request_payload(generation: AITodoGeneration) -> dict:
    """Only the requester's own assigned tasks, and only the fields the model
    needs. No assignee, department, or teammate ever crosses this boundary --
    a to-do list has exactly one owner and nothing to route."""
    tasks = Task.objects.select_related('project').filter(
        id__in=generation.source_task_ids, assigned_to_id=generation.user_id, is_deleted=False,
    )
    return {
        'generation_id': str(generation.id),
        'today': generation.window_start.isoformat(),
        'window_start': generation.window_start.isoformat(),
        'window_end': generation.window_end.isoformat(),
        'max_todos': generation.max_todos,
        'instructions': generation.instructions,
        'tasks': [
            {
                'task_id': str(task.id),
                'title': task.title,
                'description': task.description,
                'status': task.status,
                'priority': task.priority,
                'project_title': task.project.title if task.project_id else '',
                'deadline': task.deadline.date().isoformat() if task.deadline else None,
            }
            for task in tasks
        ],
    }


def _call_ai_service(payload: dict) -> dict:
    headers = {}
    if settings.WORKROOM_AI_SERVICE_TOKEN:
        headers['X-Service-Token'] = settings.WORKROOM_AI_SERVICE_TOKEN
    try:
        response = requests.post(
            f'{settings.WORKROOM_AI_SERVICE_URL}/api/v1/task-todos',
            json=payload, headers=headers, timeout=settings.WORKROOM_AI_SERVICE_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TransientAIServiceError(str(exc)) from exc

    if response.status_code == 200:
        return response.json()
    if response.status_code in (429, 503) or response.status_code >= 500:
        raise TransientAIServiceError(f'AI service returned {response.status_code}: {response.text[:500]}')
    raise PermanentTodoGenerationError(
        f'AI service rejected the request ({response.status_code}): {response.text[:500]}',
    )


def _mark_failed(generation: AITodoGeneration, error_message: str):
    generation.status = AITodoGeneration.STATUS.FAILED
    generation.completed_at = timezone.now()
    generation.error_message = error_message[:2000]
    generation.save(update_fields=['status', 'completed_at', 'error_message'])
    notify_todos_generation_failed(generation)


def _persist_todos(generation: AITodoGeneration, raw_todos: list) -> int:
    """Re-validate every proposed todo against real rows, then write the
    survivors in one transaction. Returns how many were created.

    Idempotent: a retried delivery that finds this generation already has
    todos creates nothing further (Rule 8).
    """
    with transaction.atomic():
        locked = AITodoGeneration.objects.select_for_update().get(id=generation.id)
        existing = TodoItem.objects.filter(ai_generation=locked).count()
        if existing:
            logger.info(
                'ai_todos.skipped_already_persisted',
                extra={'generation_id': str(locked.id), 'existing': existing},
            )
            return existing

        # The assignment check runs again HERE, not just at request time: the
        # task may have been reassigned away from the requester while the
        # generation was in flight, and a todo must never be created from
        # work they no longer own.
        still_assigned = dict(
            Task.objects.filter(
                id__in=locked.source_task_ids, assigned_to_id=locked.user_id, is_deleted=False,
            ).values_list('id', 'title')
        )

        # Positions are per (user, day); start each day after whatever the
        # user already has there so a generation appends rather than colliding.
        next_position = {}
        for due_date in {t.get('due_date') for t in raw_todos}:
            last = TodoItem.objects.filter(
                user_id=locked.user_id, due_date=due_date, is_deleted=False,
            ).order_by('-position').first()
            next_position[due_date] = (last.position + 1) if last else 0

        created = []
        for item in raw_todos[: locked.max_todos]:
            task_id = item.get('task_id')
            title = (item.get('title') or '').strip()
            due_date = item.get('due_date')
            if not title or task_id not in {str(k) for k in still_assigned}:
                continue
            if not due_date or not (
                locked.window_start.isoformat() <= due_date <= locked.window_end.isoformat()
            ):
                continue
            task_uuid = next(k for k in still_assigned if str(k) == task_id)
            created.append(TodoItem(
                user_id=locked.user_id,
                company_id=locked.company_id,
                task_id=task_uuid,
                task_title_snapshot=still_assigned[task_uuid],
                title=title[:255],
                notes=(item.get('notes') or '').strip(),
                due_date=due_date,
                position=next_position[due_date],
                source=TodoItem.SOURCE.AI_GENERATED,
                ai_generation=locked,
            ))
            next_position[due_date] += 1

        if created:
            TodoItem.objects.bulk_create(created)
        return len(created)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_todo_generation(self, generation_id: str):
    generation = AITodoGeneration.objects.filter(id=generation_id).select_related('user', 'company').first()
    if generation is None:
        logger.warning('ai_todos.not_found', extra={'generation_id': str(generation_id)})
        return
    if generation.status not in (AITodoGeneration.STATUS.PENDING, AITodoGeneration.STATUS.PROCESSING):
        # Already finished by an earlier delivery of this same task.
        logger.info('ai_todos.skipped_terminal_status', extra={'generation_id': str(generation_id)})
        return

    AITodoGeneration.objects.filter(id=generation.id).update(
        status=AITodoGeneration.STATUS.PROCESSING, started_at=timezone.now(),
    )

    payload = _build_request_payload(generation)
    if not payload['tasks']:
        # Every source task was reassigned or deleted between the request and
        # the worker picking it up. Nothing to decompose -- fail cleanly
        # instead of calling the provider with an empty list.
        _mark_failed(generation, 'None of the selected tasks are still assigned to you.')
        return

    try:
        response_body = _call_ai_service(payload)
    except TransientAIServiceError as exc:
        logger.warning('ai_todos.transient_failure', extra={'generation_id': str(generation.id), 'error': str(exc)})
        if self.request.retries >= self.max_retries:
            _mark_failed(generation, f'AI service unavailable after {self.max_retries} retries: {exc}')
            return
        raise self.retry(exc=exc)
    except PermanentTodoGenerationError as exc:
        logger.error('ai_todos.permanent_failure', extra={'generation_id': str(generation.id), 'error': str(exc)})
        _mark_failed(generation, str(exc))
        return

    data = response_body.get('data') or {}
    raw_todos = data.get('todos') or []
    if not raw_todos:
        _mark_failed(generation, 'AI service returned no to-dos.')
        return

    count = _persist_todos(generation, raw_todos)
    if not count:
        # Everything the model proposed failed Django's own re-validation --
        # reporting success over an empty list would be a lie (Rule 4).
        _mark_failed(generation, 'None of the generated to-dos passed validation.')
        return

    generation.status = AITodoGeneration.STATUS.COMPLETED
    generation.completed_at = timezone.now()
    generation.todo_count = count
    generation.provider = (data.get('provider') or '')[:50]
    generation.model = (data.get('model') or '')[:100]
    generation.save(update_fields=['status', 'completed_at', 'todo_count', 'provider', 'model'])
    notify_todos_generated(generation)
    logger.info('ai_todos.completed', extra={'generation_id': str(generation.id), 'todo_count': count})
