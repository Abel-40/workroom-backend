"""Task CRUD, assignment, and Kanban status API (Phases 2 & 3)."""

from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID

from ninja import Router, Schema
from projects_and_tasks import services
from projects_and_tasks.models import Task, TaskTimeLog
from pydantic import Field
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['tasks'])
auth = JWTBearerAuth()

PriorityLiteral = Literal['low', 'medium', 'high']
StatusLiteral = Literal['To Do', 'In Progress', 'In Review', 'Done']


class TaskIn(Schema):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default='No description provided', max_length=10_000)
    department_id: UUID | None = None
    task_type_id: UUID | None = None
    assigned_to_id: UUID | None = None
    priority: PriorityLiteral = 'medium'
    deadline: datetime | None = None
    estimated_time_hours: float | None = Field(default=None, gt=0, le=1000)


class TaskUpdateIn(Schema):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    department_id: UUID | None = None
    task_type_id: UUID | None = None
    priority: PriorityLiteral | None = None
    deadline: datetime | None = None
    estimated_time_hours: float | None = Field(default=None, gt=0, le=1000)


class TaskStatusIn(Schema):
    status: StatusLiteral


class TaskAssignIn(Schema):
    assigned_to_id: UUID | None = None


class TimeLogIn(Schema):
    hours: float = Field(gt=0, le=24)
    work_date: date | None = None
    description: str = Field(default='', max_length=2000)


async def task_data(task: Task) -> dict:
    return {
        'id': str(task.id),
        'project_id': str(task.project_id) if task.project_id else None,
        'department_id': str(task.department_id) if task.department_id else None,
        'task_type_id': str(task.task_type_id) if task.task_type_id else None,
        'created_by': str(task.created_by_id) if task.created_by_id else None,
        'assigned_to': str(task.assigned_to_id) if task.assigned_to_id else None,
        'title': task.title,
        'description': task.description,
        'status': task.status,
        'priority': task.priority,
        'source': task.source,
        'deadline': task.deadline.isoformat(),
        'estimated_time_hours': task.estimated_time.total_seconds() / 3600 if task.estimated_time else None,
        'spent_time_hours': await services.task_spent_hours(task),
        'created_at': task.created_at.isoformat(),
        'updated_at': task.updated_at.isoformat(),
    }


def time_log_data(log: TaskTimeLog) -> dict:
    return {
        'id': str(log.id),
        'task_id': str(log.task_id),
        'user_id': str(log.user_id) if log.user_id else None,
        'user_name': log.user.username if log.user_id else None,
        'hours': log.duration.total_seconds() / 3600,
        'work_date': log.work_date.isoformat(),
        'description': log.description,
        'created_at': log.created_at.isoformat(),
    }


def my_time_log_data(log: TaskTimeLog) -> dict:
    data = time_log_data(log)
    data.update({
        'task_title': log.task.title,
        'project_id': str(log.task.project_id) if log.task.project_id else None,
        'project_title': log.task.project.title if log.task.project_id else None,
    })
    return data


def _hours_to_duration(hours: float | None) -> timedelta | None:
    return timedelta(hours=hours) if hours is not None else None


@router.post('/projects/{project_id}/tasks/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def create_task(request, project_id: UUID, data: TaskIn):
    project, error = await services.get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to add tasks to this project.', 403, False)
    task, error = await services.create_task(
        request.auth, project,
        title=data.title, description=data.description, priority=data.priority,
        deadline=data.deadline or project.deadline,
        estimated_time=_hours_to_duration(data.estimated_time_hours),
        department_id=data.department_id, task_type_id=data.task_type_id, assigned_to_id=data.assigned_to_id,
    )
    if error == 'forbidden':
        return payload('You do not have permission to add tasks to this project.', 403, False)
    if error:
        return payload('Invalid department, task type, or assignee for this company.', 400, False, errors={error: ['Invalid value']})
    return payload('Task created successfully.', 201, True, {'task': await task_data(task)})


@router.get('/projects/{project_id}/tasks/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def list_tasks(request, project_id: UUID, status: StatusLiteral | None = None, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    project, error = await services.get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this project.', 403, False)
    queryset = Task.objects.filter(project=project, is_deleted=False).order_by('-created_at')
    if status:
        queryset = queryset.filter(status=status)
    items, meta = await paginate(queryset, page, page_size)
    return payload('Tasks retrieved successfully.', 200, True, {
        'results': [await task_data(task) for task in items], 'meta': meta,
    })


@router.get('/tasks/{task_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_task(request, task_id: UUID):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    return payload('Task retrieved successfully.', 200, True, {'task': await task_data(task)})


@router.patch('/tasks/{task_id}/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def update_task(request, task_id: UUID, data: TaskUpdateIn):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    updates = data.model_dump(exclude_unset=True)
    if 'estimated_time_hours' in updates:
        updates['estimated_time'] = _hours_to_duration(updates.pop('estimated_time_hours'))
    updated, error = await services.update_task(request.auth, task, updates)
    if error == 'forbidden':
        return payload('You do not have permission to modify this task.', 403, False)
    if error:
        return payload('Invalid department or task type for this company.', 400, False)
    return payload('Task updated successfully.', 200, True, {'task': await task_data(updated)})


@router.patch('/tasks/{task_id}/status/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def update_task_status(request, task_id: UUID, data: TaskStatusIn):
    """Dedicated endpoint for Kanban drag-and-drop: the backend remains the
    authority on the transition regardless of what the frontend allows."""
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    updated, error = await services.update_task_status(request.auth, task, data.status)
    if error == 'forbidden':
        return payload('You do not have permission to update this task.', 403, False)
    if error == 'invalid_status':
        return payload('Invalid status.', 400, False)
    return payload('Task status updated successfully.', 200, True, {'task': await task_data(updated)})


@router.post('/tasks/{task_id}/assign/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def assign_task(request, task_id: UUID, data: TaskAssignIn):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    updated, error = await services.assign_task(request.auth, task, data.assigned_to_id)
    if error == 'forbidden':
        return payload('You do not have permission to assign this task.', 403, False)
    if error == 'invalid_assignee':
        return payload('The selected user is not a member of this company.', 400, False, errors={'assigned_to_id': ['Not eligible']})
    return payload('Task assigned successfully.', 200, True, {'task': await task_data(updated)})


@router.delete('/tasks/{task_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def archive_task(request, task_id: UUID):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    if not await services.archive_task(request.auth, task):
        return payload('You do not have permission to archive this task.', 403, False)
    return payload('Task archived successfully.', 200, True)


@router.post('/tasks/{task_id}/time-logs/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def create_time_log(request, task_id: UUID, data: TimeLogIn):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    log, error = await services.create_time_log(
        request.auth, task, hours=data.hours, work_date=data.work_date, description=data.description,
    )
    if error == 'forbidden':
        return payload('You do not have permission to log time on this task.', 403, False)
    if error == 'invalid_hours':
        return payload('Hours must be greater than 0 and no more than 24.', 400, False, errors={'hours': ['Invalid value']})
    return payload('Time logged successfully.', 201, True, {
        'time_log': time_log_data(log), 'spent_time_hours': await services.task_spent_hours(task),
    })


@router.get('/tasks/{task_id}/time-logs/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def list_time_logs(request, task_id: UUID, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    queryset = TaskTimeLog.objects.filter(task=task, is_deleted=False).select_related('user').order_by('-work_date', '-created_at')
    items, meta = await paginate(queryset, page, page_size)
    return payload('Time logs retrieved successfully.', 200, True, {
        'results': [time_log_data(log) for log in items], 'meta': meta,
    })


@router.delete('/tasks/{task_id}/time-logs/{log_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def delete_time_log(request, task_id: UUID, log_id: UUID):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    ok, error = await services.delete_time_log(request.auth, task, log_id)
    if error == 'not_found':
        return payload('Time log not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to delete this time log.', 403, False)
    return payload('Time log deleted successfully.', 200, True, {'spent_time_hours': await services.task_spent_hours(task)})


@router.get('/time-logs/mine/', auth=auth, response={200: ApiResponse})
async def list_my_time_logs(
    request, start_date: date | None = None, end_date: date | None = None,
    page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
):
    """Every time log the current user has personally logged, across every
    task/project -- powers My Activity's real per-period 'Time by projects'."""
    queryset = TaskTimeLog.objects.filter(user=request.auth, is_deleted=False).select_related('user', 'task', 'task__project')
    if start_date:
        queryset = queryset.filter(work_date__gte=start_date)
    if end_date:
        queryset = queryset.filter(work_date__lte=end_date)
    queryset = queryset.order_by('-work_date', '-created_at')
    items, meta = await paginate(queryset, page, page_size)
    return payload('Time logs retrieved successfully.', 200, True, {
        'results': [my_time_log_data(log) for log in items], 'meta': meta,
    })
