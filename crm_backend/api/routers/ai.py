"""AI project-decomposition request API (Phase 5 -- generation lifecycle
only; no LLM call happens here, see ai_agent/services.py)."""

from uuid import UUID

from ai_agent import assistant_services, health_services, services
from ai_agent.models import AIAssistantQuery, AIGeneratedTask, AIGeneration, AIProjectHealthSummary, AITaskContentRegeneration
from ai_agent.tasks_regenerate import process_ai_plan_regeneration, process_task_content_regeneration
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from ninja import Router
from projects_and_tasks.services import (
    get_task_for_user,
    get_viewable_project,
    is_eligible_assignee,
    persist_ai_generated_tasks,
    user_can_manage_project,
    user_can_manage_task,
)
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate
from utils.rate_limit import rate_limit

from .tasks import task_data
from ..auth import JWTBearerAuth
from ..schemas import (
    AIAssistantIn,
    AIGeneratedTaskAssignIn,
    AIGeneratedTaskCommentIn,
    AIPlanRequestIn,
    AITaskRegenerateIn,
    ApiResponse,
)

router = Router(tags=['ai'])
auth = JWTBearerAuth()
User = get_user_model()


def generated_task_data(item: AIGeneratedTask) -> dict:
    return {
        'id': str(item.id),
        'temporary_id': item.temporary_id,
        'sequence': item.sequence,
        'title': item.title,
        'description': item.description,
        'priority': item.priority,
        'estimated_effort': item.estimated_effort,
        'dependency_temp_ids': item.dependency_temp_ids,
        'suggested_department_id': str(item.suggested_department_id) if item.suggested_department_id else None,
        'suggested_task_type_id': str(item.suggested_task_type_id) if item.suggested_task_type_id else None,
        'assigned_to_id': str(item.assigned_to_id) if item.assigned_to_id else None,
        'reviewer_comment': item.reviewer_comment,
        'comment_resolved': item.comment_resolved,
        'created_task_id': str(item.created_task_id) if item.created_task_id else None,
    }


async def generation_data(generation: AIGeneration, *, include_tasks: bool = True) -> dict:
    data = {
        'id': str(generation.id),
        'project_id': str(generation.project_id),
        'requested_by': str(generation.requested_by_id) if generation.requested_by_id else None,
        'status': generation.status,
        'provider': generation.provider,
        'model': generation.model,
        'requested_at': generation.requested_at.isoformat(),
        'started_at': generation.started_at.isoformat() if generation.started_at else None,
        'completed_at': generation.completed_at.isoformat() if generation.completed_at else None,
        'saved_at': generation.saved_at.isoformat() if generation.saved_at else None,
        'task_count': generation.task_count,
        'error_message': generation.error_message,
    }
    if include_tasks:
        data['generated_tasks'] = [generated_task_data(item) async for item in generation.generated_tasks.all()]
    return data


@router.post('/projects/{project_id}/ai-plan/', auth=auth, response={202: ApiResponse, 403: ApiResponse, 404: ApiResponse, 409: ApiResponse, 500: ApiResponse})
async def request_ai_plan(request, project_id: UUID, data: AIPlanRequestIn):
    project, error = await get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to request an AI plan for this project.', 403, False)
    generation, error = await services.request_project_plan(
        request.auth, project, prompt=data.prompt, mentioned_user_ids=data.mentioned_user_ids,
    )
    if error == 'forbidden':
        return payload('You do not have permission to request an AI plan for this project.', 403, False)
    if error == 'plan_already_saved':
        return payload('This project already has a saved AI-generated plan.', 409, False)
    if error == 'queue_failed':
        return payload(
            'The AI generation job could not be queued.', 500, False, data={'generation': await generation_data(generation)},
        )
    return payload('AI generation requested.', 202, True, {'generation': await generation_data(generation)})


@router.get('/ai/generations/{generation_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_generation(request, generation_id: UUID):
    generation, error = await services.get_generation_for_user(request.auth, generation_id)
    if error == 'not_found':
        return payload('Generation not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this generation.', 403, False)
    return payload('Generation retrieved successfully.', 200, True, {'generation': await generation_data(generation)})


@router.get('/projects/{project_id}/ai-generations/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def list_generations(request, project_id: UUID, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    project, error = await get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    queryset = AIGeneration.objects.filter(project=project).order_by('-requested_at')
    items, meta = await paginate(queryset, page, page_size)
    return payload('Generation history retrieved successfully.', 200, True, {
        'results': [await generation_data(generation, include_tasks=False) for generation in items], 'meta': meta,
    })


async def _get_generation_and_task_for_review(request, generation_id: UUID, task_id: UUID):
    """Shared lookup for the review-step endpoints below: comment, assign,
    regenerate, save. Returns (generation, generated_task, error)."""
    generation, error = await services.get_generation_for_user(request.auth, generation_id)
    if error:
        return None, None, error
    if not (generation.requested_by_id == request.auth.id or await user_can_manage_project(request.auth, generation.project)):
        return None, None, 'forbidden'
    item = await AIGeneratedTask.objects.filter(id=task_id, generation=generation).afirst()
    if item is None:
        return None, None, 'not_found'
    return generation, item, None


@router.patch(
    '/ai/generations/{generation_id}/tasks/{task_id}/comment/', auth=auth,
    response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def comment_on_generated_task(request, generation_id: UUID, task_id: UUID, data: AIGeneratedTaskCommentIn):
    generation, item, error = await _get_generation_and_task_for_review(request, generation_id, task_id)
    if error == 'not_found':
        return payload('Generation or task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to review this plan.', 403, False)
    item.reviewer_comment = data.comment
    item.comment_resolved = False
    await item.asave(update_fields=['reviewer_comment', 'comment_resolved', 'updated_at'])
    return payload('Comment saved.', 200, True, {'generated_task': generated_task_data(item)})


@router.patch(
    '/ai/generations/{generation_id}/tasks/{task_id}/assign/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def assign_generated_task(request, generation_id: UUID, task_id: UUID, data: AIGeneratedTaskAssignIn):
    generation, item, error = await _get_generation_and_task_for_review(request, generation_id, task_id)
    if error == 'not_found':
        return payload('Generation or task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to review this plan.', 403, False)
    if data.assigned_to_id is None:
        item.assigned_to = None
    else:
        candidate = await User.objects.filter(id=data.assigned_to_id).afirst()
        if candidate is None or not await is_eligible_assignee(request.auth, generation.project, candidate):
            return payload(
                'The selected user is not eligible to be assigned this task.', 400, False,
                errors={'assigned_to_id': ['Not eligible']},
            )
        item.assigned_to = candidate
    await item.asave(update_fields=['assigned_to', 'updated_at'])
    return payload('Assignee saved.', 200, True, {'generated_task': generated_task_data(item)})


@router.post(
    '/ai/generations/{generation_id}/regenerate/', auth=auth,
    response={202: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse, 500: ApiResponse},
)
async def regenerate_generated_plan(request, generation_id: UUID):
    generation, error = await services.get_generation_for_user(request.auth, generation_id)
    if error == 'not_found':
        return payload('Generation not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to review this plan.', 403, False)
    if not (generation.requested_by_id == request.auth.id or await user_can_manage_project(request.auth, generation.project)):
        return payload('You do not have permission to review this plan.', 403, False)
    if generation.status != AIGeneration.STATUS.COMPLETED or generation.saved_at is not None:
        return payload('This plan cannot be regenerated right now.', 400, False)
    if not await generation.generated_tasks.filter(comment_resolved=False).aexists():
        return payload('No tasks have pending comments to regenerate.', 400, False)

    await AIGeneration.objects.filter(id=generation.id).aupdate(status=AIGeneration.STATUS.PROCESSING)
    generation.status = AIGeneration.STATUS.PROCESSING
    try:
        await sync_to_async(process_ai_plan_regeneration.delay, thread_sensitive=True)(str(generation.id))
    except Exception:
        await AIGeneration.objects.filter(id=generation.id).aupdate(
            status=AIGeneration.STATUS.COMPLETED, error_message='Failed to queue the plan regeneration job.',
        )
        return payload('The plan regeneration job could not be queued.', 500, False)
    return payload('Plan regeneration requested.', 202, True, {'generation': await generation_data(generation)})


@router.post(
    '/ai/generations/{generation_id}/save/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse, 409: ApiResponse},
)
async def save_generated_plan(request, generation_id: UUID):
    generation, error = await services.get_generation_for_user(request.auth, generation_id)
    if error == 'not_found':
        return payload('Generation not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to review this plan.', 403, False)
    if not (generation.requested_by_id == request.auth.id or await user_can_manage_project(request.auth, generation.project)):
        return payload('You do not have permission to review this plan.', 403, False)
    if generation.status != AIGeneration.STATUS.COMPLETED:
        return payload('This plan is not ready to be saved.', 400, False)
    if generation.saved_at is not None:
        return payload('This plan has already been saved.', 409, False)

    try:
        created_tasks, invalid_assignee_temp_ids = await sync_to_async(persist_ai_generated_tasks, thread_sensitive=True)(generation)
    except ValueError as exc:
        return payload(str(exc), 400, False)

    return payload('Plan saved to the project backlog.', 200, True, {
        'tasks': [task_data(task) for task in created_tasks],
        'invalid_assignee_temp_ids': invalid_assignee_temp_ids,
    })


@router.post(
    '/tasks/{task_id}/regenerate-ai-content/', auth=auth,
    response={202: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse, 500: ApiResponse},
)
async def regenerate_task_ai_content(request, task_id: UUID, data: AITaskRegenerateIn):
    task, error = await get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    if not await user_can_manage_task(request.auth, task):
        return payload('You do not have permission to modify this task.', 403, False)
    if task.source != task.SOURCE.AI_GENERATED:
        return payload('Only AI-generated tasks can have their AI content regenerated.', 400, False)

    regeneration = await AITaskContentRegeneration.objects.acreate(
        task=task, requested_by=request.auth, instructions=data.instructions,
    )
    try:
        await sync_to_async(process_task_content_regeneration.delay, thread_sensitive=True)(str(regeneration.id))
    except Exception:
        await AITaskContentRegeneration.objects.filter(id=regeneration.id).aupdate(
            status=AITaskContentRegeneration.STATUS.FAILED, error_message='Failed to queue the regeneration job.',
        )
        return payload('The task content regeneration job could not be queued.', 500, False)
    return payload('Task content regeneration requested.', 202, True, {
        'task_regeneration': task_regeneration_data(regeneration),
    })


def task_regeneration_data(regeneration: AITaskContentRegeneration) -> dict:
    return {
        'id': str(regeneration.id),
        'task_id': str(regeneration.task_id),
        'status': regeneration.status,
        'provider': regeneration.provider,
        'model': regeneration.model,
        'requested_at': regeneration.requested_at.isoformat(),
        'started_at': regeneration.started_at.isoformat() if regeneration.started_at else None,
        'completed_at': regeneration.completed_at.isoformat() if regeneration.completed_at else None,
        'error_message': regeneration.error_message,
        # Included once COMPLETED so the frontend can patch its task state
        # straight from this poll response -- no extra fetch needed.
        'task': task_data(regeneration.task) if regeneration.status == AITaskContentRegeneration.STATUS.COMPLETED else None,
    }


@router.get('/ai/task-regenerations/{regeneration_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_task_regeneration(request, regeneration_id: UUID):
    regeneration = await AITaskContentRegeneration.objects.select_related('task').filter(id=regeneration_id).afirst()
    if regeneration is None:
        return payload('Regeneration not found.', 404, False)
    task, error = await get_task_for_user(request.auth, regeneration.task_id)
    if error:
        return payload('You do not have permission to view this task.', 403, False)
    return payload('Task regeneration retrieved successfully.', 200, True, {'task_regeneration': task_regeneration_data(regeneration)})


def assistant_query_data(query: AIAssistantQuery) -> dict:
    return {
        'id': str(query.id),
        'project_id': str(query.project_id),
        'requested_by': str(query.requested_by_id) if query.requested_by_id else None,
        'question': query.question,
        'reference_url': query.reference_url,
        'status': query.status,
        'provider': query.provider,
        'model': query.model,
        'answer': query.answer,
        'refused': query.refused,
        'requested_at': query.requested_at.isoformat(),
        'started_at': query.started_at.isoformat() if query.started_at else None,
        'completed_at': query.completed_at.isoformat() if query.completed_at else None,
        'error_message': query.error_message,
    }


@router.post(
    '/projects/{project_id}/ai-assistant/', auth=auth,
    response={202: ApiResponse, 403: ApiResponse, 404: ApiResponse, 500: ApiResponse},
)
@rate_limit('ai_assistant_query', limit=20, window_seconds=3600, key_func=lambda r: str(r.auth.id))
async def request_ai_assistant(request, project_id: UUID, data: AIAssistantIn):
    project, error = await get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to ask the assistant about this project.', 403, False)
    reference_url = str(data.reference_url) if data.reference_url else None
    query, error = await assistant_services.request_assistant_query(request.auth, project, data.question, reference_url)
    if error == 'forbidden':
        return payload('You do not have permission to ask the assistant about this project.', 403, False)
    if error == 'queue_failed':
        return payload(
            'The assistant query job could not be queued.', 500, False, data={'assistant_query': assistant_query_data(query)},
        )
    return payload('Assistant query requested.', 202, True, {'assistant_query': assistant_query_data(query)})


@router.get('/ai/assistant-queries/{query_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_assistant_query(request, query_id: UUID):
    query, error = await assistant_services.get_assistant_query_for_user(request.auth, query_id)
    if error == 'not_found':
        return payload('Assistant query not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this assistant query.', 403, False)
    return payload('Assistant query retrieved successfully.', 200, True, {'assistant_query': assistant_query_data(query)})


@router.get(
    '/projects/{project_id}/ai-assistant-queries/', auth=auth,
    response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def list_assistant_queries(request, project_id: UUID, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    project, error = await get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    queryset = AIAssistantQuery.objects.filter(project=project).order_by('-requested_at')
    items, meta = await paginate(queryset, page, page_size)
    return payload('Assistant query history retrieved successfully.', 200, True, {
        'results': [assistant_query_data(query) for query in items], 'meta': meta,
    })


def health_summary_data(summary: AIProjectHealthSummary) -> dict:
    return {
        'id': str(summary.id),
        'project_id': str(summary.project_id),
        'requested_by': str(summary.requested_by_id) if summary.requested_by_id else None,
        'status': summary.status,
        'provider': summary.provider,
        'model': summary.model,
        'summary': summary.summary,
        'risk_level': summary.risk_level,
        'requested_at': summary.requested_at.isoformat(),
        'started_at': summary.started_at.isoformat() if summary.started_at else None,
        'completed_at': summary.completed_at.isoformat() if summary.completed_at else None,
        'error_message': summary.error_message,
    }


@router.post(
    '/projects/{project_id}/ai-health-summary/', auth=auth,
    response={202: ApiResponse, 403: ApiResponse, 404: ApiResponse, 500: ApiResponse},
)
@rate_limit('ai_health_summary', limit=6, window_seconds=3600, key_func=lambda r: str(r.auth.id))
async def request_ai_health_summary(request, project_id: UUID):
    project, error = await get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to request a health summary for this project.', 403, False)
    summary, error = await health_services.request_health_summary(request.auth, project)
    if error == 'forbidden':
        return payload('You do not have permission to request a health summary for this project.', 403, False)
    if error == 'queue_failed':
        return payload(
            'The health summary job could not be queued.', 500, False, data={'health_summary': health_summary_data(summary)},
        )
    return payload('Health summary requested.', 202, True, {'health_summary': health_summary_data(summary)})


@router.get('/ai/health-summaries/{summary_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_health_summary(request, summary_id: UUID):
    summary, error = await health_services.get_health_summary_for_user(request.auth, summary_id)
    if error == 'not_found':
        return payload('Health summary not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this health summary.', 403, False)
    return payload('Health summary retrieved successfully.', 200, True, {'health_summary': health_summary_data(summary)})


@router.get(
    '/projects/{project_id}/ai-health-summaries/', auth=auth,
    response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def list_health_summaries(request, project_id: UUID, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    project, error = await get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    queryset = AIProjectHealthSummary.objects.filter(project=project).order_by('-requested_at')
    items, meta = await paginate(queryset, page, page_size)
    return payload('Health summary history retrieved successfully.', 200, True, {
        'results': [health_summary_data(summary) for summary in items], 'meta': meta,
    })
