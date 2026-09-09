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
from collections import deque

import requests
from celery import shared_task
from departments_and_teams.models import Department
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from notifications_and_activity.services import notify_ai_generation_completed, notify_ai_generation_failed
from projects_and_tasks.models import TaskType
from workforce.models import AssignmentPolicy
from workforce.workload import evaluate_assignment_sync

from .context import build_generation_context, estimate_cost, resolve_assignee_ref
from .models import AIGeneratedTask, AIGeneration

User = get_user_model()

logger = logging.getLogger(__name__)


class TransientAIServiceError(Exception):
    """Network error, timeout, or 5xx/429 from the AI service -- safe to retry."""


class PermanentAIGenerationError(Exception):
    """The AI service rejected the request or returned invalid output."""


def _build_request_payload(generation: AIGeneration) -> dict:
    """Delegates to the allow-list serializer.

    Kept as a named seam rather than inlining the call, because this is the
    one function in the pipeline that decides what leaves the building and it
    should stay easy to find.
    """
    return build_generation_context(generation, requester_timezone=_requester_timezone(generation))


def _requester_timezone(generation: AIGeneration) -> str:
    """The requester's own timezone, so "by Friday" means their Friday."""
    return getattr(generation.requested_by, 'timezone', '') or 'UTC'


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
    generation.provider = (plan_data.get('provider') or '')[:50]
    generation.model = (plan_data.get('model') or '')[:100]
    _record_usage(generation, plan_data)
    generation.save(update_fields=[
        'status', 'completed_at', 'task_count', 'provider', 'model',
        'input_tokens', 'output_tokens', 'cost', 'fallback_from',
    ])
    notify_ai_generation_completed(generation)
    logger.info(
        'ai_generation.completed',
        extra={
            'generation_id': str(generation.id), 'task_count': created_count,
            'provider': generation.provider, 'model': generation.model,
            'input_tokens': generation.input_tokens, 'output_tokens': generation.output_tokens,
        },
    )


def _record_usage(generation: AIGeneration, plan_data: dict):
    """Attach token counts and a costing to the generation.

    Recorded whether or not anything bills on it yet, because usage cannot be
    reconstructed after the fact -- the request is gone, and the only place
    the numbers ever existed was this response.

    A provider that reports nothing leaves nulls rather than zeros, and an
    unpriced model leaves a null cost with real token counts. Both say "we do
    not know" rather than "it was free"; the tokens are the part that matters
    most, because a price list can be applied retroactively and a token count
    cannot be invented.
    """
    usage = plan_data.get('usage') or {}
    generation.input_tokens = _positive_int(usage.get('input_tokens'))
    generation.output_tokens = _positive_int(usage.get('output_tokens'))
    generation.cost = estimate_cost(
        generation.provider, generation.model,
        generation.input_tokens or 0, generation.output_tokens or 0,
    )
    # The provider that answered is not always the one we asked. Recorded so
    # "it worked" and "it worked on the second provider" stay distinguishable
    # -- only one of those means the primary is healthy.
    requested = (settings.WORKROOM_AI_PRIMARY_PROVIDER or '').lower()
    if requested and generation.provider and generation.provider.lower() != requested:
        generation.fallback_from = requested[:50]


def _positive_int(value):
    """A token count, or None. Anything a provider sends that is not a
    non-negative integer is treated as "not reported" rather than coerced --
    a silently-zeroed count is indistinguishable from a free call."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _assert_plan_is_a_dag(tasks_data):
    """Every dependency edge, checked for cycles across the whole plan.

    A plan whose tasks depend on each other in a loop can never start: there
    is no first task. Catching it here means the reviewer sees a failed
    generation rather than a board that looks fine until somebody tries to
    move something. Kahn's algorithm -- if any node still has an incoming edge
    when the queue empties, those nodes are the cycle.
    """
    dependencies = {item['temporary_id']: list(item.get('dependency_ids') or []) for item in tasks_data}
    indegree = {temp_id: 0 for temp_id in dependencies}
    dependents = {temp_id: [] for temp_id in dependencies}
    for temp_id, deps in dependencies.items():
        for dep in deps:
            indegree[temp_id] += 1
            dependents[dep].append(temp_id)

    queue = deque(temp_id for temp_id, count in indegree.items() if count == 0)
    settled = 0
    while queue:
        current = queue.popleft()
        settled += 1
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)

    if settled != len(dependencies):
        cycle = sorted(temp_id for temp_id, count in indegree.items() if count > 0)
        raise ValueError(f'AI plan contains a dependency cycle involving: {", ".join(cycle)}')


def _over_capacity(company, user_id) -> bool:
    """Whether assigning one more task to this person crosses a configured
    limit. Used to *flag* a suggestion, never to drop it -- see the docstring
    of _store_generated_tasks_for_review."""
    policy = AssignmentPolicy.objects.filter(company=company).first()
    if policy is None or not policy.enabled:
        return False
    candidate = User.objects.filter(id=user_id).first()
    if candidate is None:
        return False
    decision = evaluate_assignment_sync(company, candidate, policy=policy)
    return bool(decision.breached)


def _store_generated_tasks_for_review(generation: AIGeneration, plan_data: dict) -> int:
    """Validate the AI's plan against this company's real records and store it
    as draft AIGeneratedTask rows for human review -- deliberately does NOT
    create real Task rows (see module docstring). Django re-validates
    independently of the AI service's own validation (Rule 9).

    Structural failures fail the whole generation: a department or task type
    outside the company, a dependency naming a task that is not in the plan,
    a repeated temporary_id, or a dependency cycle.

    Auxiliary ones do not. An assignee suggestion whose ref cannot be resolved
    is dropped; one that would put somebody over a workload limit is
    **flagged and kept**, because the reviewer may know something the numbers
    do not, and silently removing it would hide that the suggestion was ever
    made.
    """
    project = generation.project
    company = project.company
    tasks_data = plan_data.get('tasks') or []
    if not tasks_data:
        raise ValueError('AI service returned an empty task list.')

    # Never trust the AI service to have honored max_tasks on its own
    # (Rule 10) -- truncate here regardless of what it returned.
    if generation.max_tasks:
        tasks_data = tasks_data[:generation.max_tasks]

    # Names, matched against this company's own catalogs. Both are unique per
    # company at the database level, so a name resolves to exactly one row --
    # which is why the payload can send names and never ids.
    departments_by_name = {
        name.lower(): dept_id
        for dept_id, name in Department.objects.filter(company=company).values_list('id', 'name')
    }
    task_types_by_name = {
        name.lower(): type_id
        for type_id, name in TaskType.objects.filter(company=company).values_list('id', 'name')
    }

    known_temp_ids = set()
    for item in tasks_data:
        if not item.get('temporary_id') or not item.get('title'):
            raise ValueError('AI-generated task is missing temporary_id or title.')
        if item['temporary_id'] in known_temp_ids:
            raise ValueError(f"AI plan reuses temporary_id '{item['temporary_id']}'.")
        known_temp_ids.add(item['temporary_id'])

    for item in tasks_data:
        for dep in item.get('dependency_ids') or []:
            if dep not in known_temp_ids:
                raise ValueError(f"Task '{item['temporary_id']}' references an unknown dependency id.")
    _assert_plan_is_a_dag(tasks_data)

    # Every generated task inherits the project deadline, so the task
    # invariant (task.deadline <= project.deadline) holds by construction --
    # asserted here rather than assumed, because the day somebody lets the AI
    # propose per-task deadlines this is the check that has to already exist.
    if project.deadline is None:
        raise ValueError('Cannot store a generated plan for a project with no deadline.')

    over_capacity_cache = {}
    to_create = []
    for item in tasks_data:
        department_id = None
        raw_department = item.get('suggested_department')
        if raw_department:
            department_id = departments_by_name.get(str(raw_department).strip().lower())
            if department_id is None:
                raise ValueError(f"Task '{item['temporary_id']}' suggested a department outside this company.")

        task_type_id = None
        raw_task_type = item.get('suggested_task_type')
        if raw_task_type:
            task_type_id = task_types_by_name.get(str(raw_task_type).strip().lower())
            if task_type_id is None:
                raise ValueError(f"Task '{item['temporary_id']}' suggested a task type outside this company.")

        # The AI works in opaque refs and never sees or returns a user id. An
        # unresolvable ref -- invented, or copied from another generation --
        # is dropped rather than failing the plan: a human confirms every
        # assignment either way. The rationale goes with it, because a stated
        # reason for a suggestion that no longer exists is worse than none.
        assignee_id = None
        rationale = ''
        over_capacity = False
        raw_ref = item.get('suggested_assignee_ref')
        if raw_ref:
            assignee_id = resolve_assignee_ref(generation, raw_ref)
            if assignee_id is None:
                logger.warning(
                    'ai_generation.suggested_assignee_ref_unresolved',
                    extra={'generation_id': str(generation.id), 'temporary_id': item['temporary_id']},
                )
            else:
                rationale = (item.get('suggested_assignee_rationale') or '')[:300]
                if assignee_id not in over_capacity_cache:
                    over_capacity_cache[assignee_id] = _over_capacity(company, assignee_id)
                over_capacity = over_capacity_cache[assignee_id]

        priority = item.get('priority') or AIGeneratedTask.PRIORITY.MEDIUM
        if priority not in AIGeneratedTask.PRIORITY.values:
            priority = AIGeneratedTask.PRIORITY.MEDIUM

        to_create.append(AIGeneratedTask(
            generation=generation, temporary_id=item['temporary_id'], sequence=item.get('sequence') or 0,
            title=item['title'][:255], description=item.get('description') or '',
            priority=priority, estimated_effort=(item.get('estimated_effort') or '')[:100],
            suggested_assignee_id=assignee_id,
            suggested_assignee_rationale=rationale,
            suggested_assignee_over_capacity=over_capacity,
            dependency_temp_ids=list(item.get('dependency_ids') or []),
            suggested_department_id=department_id, suggested_task_type_id=task_type_id,
        ))

    AIGeneratedTask.objects.bulk_create(to_create)
    return len(to_create)
