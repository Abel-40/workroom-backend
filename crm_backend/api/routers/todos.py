"""Personal to-do API.

Every endpoint here is scoped to ``request.auth`` and nothing else -- there
is no role check because no role grants access to another person's todos
(see todos/services.py). Company membership is still resolved on write, but
only to stamp the row's tenant, never to widen who can read it.
"""

from datetime import date
from typing import Literal
from uuid import UUID

from ai_agent.models import AITodoGeneration
from ai_agent.tasks_todos import process_todo_generation
from asgiref.sync import sync_to_async
from company.services import get_member_company
from ninja import Router, Schema
from pydantic import Field
from todos import services
from todos.models import TodoItem
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate
from utils.rate_limit import rate_limit

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['todos'])
auth = JWTBearerAuth()


class TodoIn(Schema):
    title: str = Field(min_length=1, max_length=255)
    notes: str = Field(default='', max_length=5000)
    # Required, never defaulted: the product rule is that the user picks the
    # day. Silently defaulting to today would make "which day is this for?"
    # a question the UI could skip asking.
    due_date: date
    task_id: UUID | None = None


class TodoUpdateIn(Schema):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=5000)
    due_date: date | None = None
    position: int | None = Field(default=None, ge=0, le=10_000)
    is_done: bool | None = None


DUE_DATE_ERRORS = {
    'due_date_required': 'Pick the day this belongs to.',
    'due_date_too_far': 'That due date is too far in the future.',
}


def todo_data(todo: TodoItem, *, viewer) -> dict:
    data = {
        'id': str(todo.id),
        'title': todo.title,
        'notes': todo.notes,
        'due_date': todo.due_date.isoformat(),
        'position': todo.position,
        'is_done': todo.is_done,
        'completed_at': todo.completed_at.isoformat() if todo.completed_at else None,
        'source': todo.source,
        'created_at': todo.created_at.isoformat(),
        'updated_at': todo.updated_at.isoformat(),
        # Always present so the UI can show what the todo was about even
        # after the link is revoked or the task is gone.
        'task_title': todo.task_title_snapshot,
        'task_id': None,
    }
    if services.task_link_is_live(todo, viewer):
        data['task_id'] = str(todo.task_id)
        data['task_status'] = todo.task.status
    return data


@router.get('/', auth=auth, response={200: ApiResponse})
async def list_todos(
    request,
    scope: str = 'all',
    include_done: bool = False,
    due_from: date | None = None,
    due_to: date | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
):
    """Nearest first, always -- see todos/models.py Meta.ordering. ``scope``
    is one of all/due/today/overdue/upcoming; an unrecognised value falls
    through to 'all' rather than erroring, since it only ever narrows."""
    queryset = services.list_todos(
        request.auth, scope=scope, include_done=include_done, due_from=due_from, due_to=due_to,
    )
    items, meta = await paginate(queryset, page, page_size)
    return payload('Todos retrieved successfully.', 200, True, {
        'results': [todo_data(item, viewer=request.auth) for item in items],
        'meta': meta,
        'today': services.user_today(request.auth).isoformat(),
    })


@router.get('/summary/', auth=auth, response={200: ApiResponse})
async def todo_summary(request):
    counts = await services.summarize(request.auth)
    return payload('Todo summary retrieved successfully.', 200, True, {
        **counts, 'today': services.user_today(request.auth).isoformat(),
    })


@router.post('/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 404: ApiResponse})
async def create_todo(request, data: TodoIn):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)

    task = None
    if data.task_id:
        task, error = await services.get_assignable_task(request.auth, data.task_id)
        if error:
            # 'not_found' and 'not_assigned' collapse to one response on
            # purpose (todos/services.py): a non-assignee must not be able to
            # tell an unassigned task from a nonexistent one.
            return payload('That task is not assigned to you.', 404, False)

    todo, error = await services.create_todo(
        request.auth, company, title=data.title, notes=data.notes, due_date=data.due_date, task=task,
    )
    if error:
        return payload(DUE_DATE_ERRORS.get(error, 'Validation error'), 400, False)
    return payload('Todo created successfully.', 201, True, {'todo': todo_data(todo, viewer=request.auth)})


# --------------------------------------------------------------------------
# AI generation
# --------------------------------------------------------------------------
# Generate -> Validate -> Persist. This endpoint only ever resolves which of
# the CALLER'S OWN assigned tasks may be used and enqueues the job; the
# worker (ai_agent/tasks_todos.py) re-validates everything the AI service
# returns against real rows before a todo exists.

class TodoGenerateIn(Schema):
    mode: Literal['today', 'task'] = 'today'
    # Required when mode='task'; ignored otherwise.
    task_id: UUID | None = None
    # How many days to spread a single task's checklist over. Ignored for
    # mode='today', which is one day by definition.
    days: int = Field(default=7, ge=1, le=14)
    instructions: str = Field(default='', max_length=2000)
    max_todos: int = Field(default=10, ge=1, le=30)


def generation_data(generation) -> dict:
    return {
        'id': str(generation.id),
        'mode': generation.mode,
        'status': generation.status,
        'task_id': str(generation.task_id) if generation.task_id else None,
        'window_start': generation.window_start.isoformat(),
        'window_end': generation.window_end.isoformat(),
        'todo_count': generation.todo_count,
        'requested_at': generation.requested_at.isoformat(),
        'completed_at': generation.completed_at.isoformat() if generation.completed_at else None,
        # Failure text is written by Django itself (never a raw provider
        # body -- see ai_agent/tasks_todos.py), so it is safe to surface.
        'error_message': generation.error_message,
    }


@router.post(
    '/generate/', auth=auth,
    response={202: ApiResponse, 400: ApiResponse, 404: ApiResponse, 409: ApiResponse},
)
@rate_limit('todo_generate', limit=20, window_seconds=3600, key_func=lambda r: str(r.auth.id))
async def generate_todos(request, data: TodoGenerateIn):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)

    in_flight = await services.find_in_flight_generation(request.auth)
    if in_flight is not None:
        return payload("You already have a to-do generation running.", 409, False, {
            'generation': generation_data(in_flight),
        })

    task = None
    if data.mode == 'task':
        if data.task_id is None:
            return payload('Choose a task to build to-dos from.', 400, False)
        task, error = await services.get_assignable_task(request.auth, data.task_id)
        if error:
            return payload('That task is not assigned to you.', 404, False)

    tasks, error = await services.resolve_generation_sources(
        request.auth, company, mode=data.mode, task=task,
    )
    if error == 'no_assigned_tasks':
        return payload('You have no open tasks assigned to you right now.', 400, False)

    window_start, window_end = await services.resolve_generation_window(
        request.auth, mode=data.mode, task=task, days=data.days,
    )
    generation = await AITodoGeneration.objects.acreate(
        user=request.auth, company=company, mode=data.mode, task=task,
        source_task_ids=[str(t.id) for t in tasks],
        window_start=window_start, window_end=window_end,
        instructions=data.instructions.strip(), max_todos=data.max_todos,
    )
    # thread_sensitive=True: under CELERY_TASK_ALWAYS_EAGER (tests) .delay()
    # runs the task body inline, and its queries must stay on this request's
    # connection so they can see the row just created above.
    await sync_to_async(process_todo_generation.delay, thread_sensitive=True)(str(generation.id))
    return payload('Generating your to-dos.', 202, True, {'generation': generation_data(generation)})


@router.get('/generations/{generation_id}/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def get_todo_generation(request, generation_id: UUID):
    generation, error = await services.get_generation_for_user(request.auth, generation_id)
    if error == 'not_found':
        return payload('Generation not found.', 404, False)
    return payload('Generation retrieved successfully.', 200, True, {
        'generation': generation_data(generation),
    })


@router.post('/generations/{generation_id}/dismiss/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def dismiss_todo_generation(request, generation_id: UUID):
    """Bulk undo for a generation the owner didn't find useful. Leaves
    anything they already completed alone (todos/services.py)."""
    generation, error = await services.get_generation_for_user(request.auth, generation_id)
    if error == 'not_found':
        return payload('Generation not found.', 404, False)
    dismissed = await services.dismiss_generation(request.auth, generation)
    return payload('Generated to-dos dismissed.', 200, True, {'dismissed': dismissed})


# --------------------------------------------------------------------------
# Parameterised routes last
# --------------------------------------------------------------------------
# Ninja matches routes in declaration order and a typed path parameter
# accepts any non-slash segment, so '/{todo_id}/' declared above would
# swallow every literal sibling below it -- '/generate/' would resolve to
# the todo detail route and 405. Keep these at the bottom of the file.

@router.patch('/{todo_id}/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 404: ApiResponse})
async def update_todo(request, todo_id: UUID, data: TodoUpdateIn):
    todo, error = await services.get_todo_for_user(request.auth, todo_id)
    if error == 'not_found':
        return payload('Todo not found.', 404, False)
    updated, error = await services.update_todo(request.auth, todo, data.dict(exclude_unset=True))
    if error:
        return payload(DUE_DATE_ERRORS.get(error, 'Validation error'), 400, False)
    return payload('Todo updated successfully.', 200, True, {'todo': todo_data(updated, viewer=request.auth)})


@router.delete('/{todo_id}/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def delete_todo(request, todo_id: UUID):
    todo, error = await services.get_todo_for_user(request.auth, todo_id)
    if error == 'not_found':
        return payload('Todo not found.', 404, False)
    await services.delete_todo(request.auth, todo)
    return payload('Todo deleted successfully.', 200, True)
