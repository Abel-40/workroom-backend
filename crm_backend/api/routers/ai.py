"""AI project-decomposition request API (Phase 5 -- generation lifecycle
only; no LLM call happens here, see ai_agent/services.py)."""

from uuid import UUID

from ai_agent import assistant_services, health_services, services
from ai_agent.models import AIAssistantQuery, AIGeneration, AIProjectHealthSummary
from ninja import Router
from projects_and_tasks.services import get_viewable_project
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate
from utils.rate_limit import rate_limit

from ..auth import JWTBearerAuth
from ..schemas import AIAssistantIn, ApiResponse

router = Router(tags=['ai'])
auth = JWTBearerAuth()


def generation_data(generation: AIGeneration) -> dict:
    return {
        'id': str(generation.id),
        'project_id': str(generation.project_id),
        'requested_by': str(generation.requested_by_id) if generation.requested_by_id else None,
        'status': generation.status,
        'provider': generation.provider,
        'model': generation.model,
        'requested_at': generation.requested_at.isoformat(),
        'started_at': generation.started_at.isoformat() if generation.started_at else None,
        'completed_at': generation.completed_at.isoformat() if generation.completed_at else None,
        'task_count': generation.task_count,
        'error_message': generation.error_message,
    }


@router.post('/projects/{project_id}/ai-plan/', auth=auth, response={202: ApiResponse, 403: ApiResponse, 404: ApiResponse, 500: ApiResponse})
async def request_ai_plan(request, project_id: UUID):
    project, error = await get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to request an AI plan for this project.', 403, False)
    generation, error = await services.request_project_plan(request.auth, project)
    if error == 'forbidden':
        return payload('You do not have permission to request an AI plan for this project.', 403, False)
    if error == 'queue_failed':
        return payload(
            'The AI generation job could not be queued.', 500, False, data={'generation': generation_data(generation)},
        )
    return payload('AI generation requested.', 202, True, {'generation': generation_data(generation)})


@router.get('/ai/generations/{generation_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_generation(request, generation_id: UUID):
    generation, error = await services.get_generation_for_user(request.auth, generation_id)
    if error == 'not_found':
        return payload('Generation not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this generation.', 403, False)
    return payload('Generation retrieved successfully.', 200, True, {'generation': generation_data(generation)})


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
        'results': [generation_data(generation) for generation in items], 'meta': meta,
    })


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
