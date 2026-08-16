"""AI project-decomposition request API (Phase 5 -- generation lifecycle
only; no LLM call happens here, see ai_agent/services.py)."""

from uuid import UUID

from ai_agent import services
from ai_agent.models import AIGeneration
from ninja import Router
from projects_and_tasks.services import get_viewable_project
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

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
