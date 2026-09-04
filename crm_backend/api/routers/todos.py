"""Personal to-do API.

Every endpoint here is scoped to ``request.auth`` and nothing else -- there
is no role check because no role grants access to another person's todos
(see todos/services.py). Company membership is still resolved on write, but
only to stamp the row's tenant, never to widen who can read it.
"""

from datetime import date
from uuid import UUID

from company.services import get_member_company
from ninja import Router, Schema
from pydantic import Field
from todos import services
from todos.models import TodoItem
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate

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
