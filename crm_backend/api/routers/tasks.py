"""Task CRUD, assignment, and Kanban status API (Phases 2 & 3)."""

from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID

from company.services import get_member_company
from ninja import File, Form, Router, Schema
from ninja.files import UploadedFile
from projects_and_tasks import services
from projects_and_tasks.models import Attachment, Task, TaskApproval, TaskTimeLog
from pydantic import Field
from todos import services as todo_services
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
    # Required, not defaulted to the project's own deadline: a task's
    # deadline must be strictly before its project's (see
    # projects_and_tasks.services.create_task), so silently defaulting it to
    # an equal value would always fail. Forces a real per-task choice instead.
    deadline: datetime
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


class TaskRejectIn(Schema):
    comment: str = Field(min_length=1, max_length=2000)


class DeadlineExtendIn(Schema):
    deadline: datetime


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
        deadline=data.deadline,
        estimated_time=_hours_to_duration(data.estimated_time_hours),
        department_id=data.department_id, task_type_id=data.task_type_id, assigned_to_id=data.assigned_to_id,
    )
    if error == 'forbidden':
        return payload('You do not have permission to add tasks to this project.', 403, False)
    if error == 'invalid_deadline':
        return payload(
            "The task deadline must be before the project's deadline.", 400, False,
            errors={'deadline': ['Must be earlier than the project deadline']},
        )
    if error == 'project_completed':
        return payload(
            'This project is marked Done -- reopen it before adding new tasks.', 400, False,
        )
    if error == 'ineligible_assignee':
        return payload(
            "That person isn't eligible for this project (outside its department/team).", 400, False,
            errors={'assigned_to_id': ['Not eligible for this project']},
        )
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


@router.get('/tasks/assigned-to-me/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def list_tasks_assigned_to_me(
    request, open_only: bool = True, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
):
    """The caller's own assigned work, across every project, nearest deadline
    first. Scoped by assignment rather than by view permission: this is the
    "what am I actually on the hook for" list that the personal to-do screen
    and its AI generation build from, so it must never include work the
    caller merely has permission to look at.

    Declared here rather than under /tasks/{task_id}/ below because Ninja
    matches routes in declaration order -- a literal path registered after a
    parameterised sibling would be swallowed by it.
    """
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    queryset = todo_services.list_assigned_tasks(request.auth, company, open_only=open_only)
    items, meta = await paginate(queryset, page, page_size)
    results = []
    for task in items:
        data = await task_data(task)
        data['project_title'] = task.project.title if task.project_id else None
        results.append(data)
    return payload('Assigned tasks retrieved successfully.', 200, True, {'results': results, 'meta': meta})


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
    if error == 'invalid_deadline':
        return payload(
            "The task deadline must be before the project's deadline.", 400, False,
            errors={'deadline': ['Must be earlier than the project deadline']},
        )
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
    if error == 'invalid_transition':
        return payload(
            'Done and In Review can only be reached through the approval workflow '
            '(see /submit-for-approval/, /approve/, and /reject/).', 400, False,
        )
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
    if error == 'ineligible_assignee':
        return payload(
            "That person isn't eligible for this project (outside its department/team).", 400, False,
            errors={'assigned_to_id': ['Not eligible for this project']},
        )
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


# --------------------------------------------------------------------------
# Approval workflow -- evidence submission, approve/reject, and deadline
# extension. Done/In Review are reachable ONLY through these endpoints (see
# update_task_status above and projects_and_tasks.services for the rules).
# --------------------------------------------------------------------------

def _evidence_data(attachment: Attachment) -> dict:
    data = {'id': str(attachment.id), 'type': attachment.type, 'name': attachment.name}
    if attachment.type == Attachment.ATTACHMENT_TYPE.FILE:
        data['download_url'] = f'/documents/{attachment.id}/download/'
    elif attachment.type == Attachment.ATTACHMENT_TYPE.LINK:
        data['url'] = attachment.url
    elif attachment.type == Attachment.ATTACHMENT_TYPE.PAGE:
        data['page_id'] = str(attachment.page_id) if attachment.page_id else None
    return data


async def _approval_data(approval: TaskApproval, viewer) -> dict:
    """rejection_comment is included ONLY for the row's own submitted_by --
    never for the approver or any other caller, even though they can see
    every other field on the same row (see reject_task_approval)."""
    evidence = [_evidence_data(a) async for a in approval.evidence.filter(is_deleted=False)]
    visible_comment = approval.submitted_by_id == viewer.id and bool(approval.rejection_comment)
    return {
        'id': str(approval.id),
        'task_id': str(approval.task_id),
        'submitted_by': str(approval.submitted_by_id) if approval.submitted_by_id else None,
        'submitted_at': approval.submitted_at.isoformat(),
        'status': approval.status,
        'decided_by': str(approval.decided_by_id) if approval.decided_by_id else None,
        'decided_at': approval.decided_at.isoformat() if approval.decided_at else None,
        'rejection_comment': approval.rejection_comment if visible_comment else None,
        'evidence': evidence,
    }


@router.post(
    '/tasks/{task_id}/submit-for-approval/', auth=auth,
    response={202: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def submit_task_for_approval(
    request, task_id: UUID,
    files: list[UploadedFile] = File(default=[]),
    links: list[str] = Form(default=[]),
    page_ids: list[UUID] = Form(default=[]),
):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    approval, error = await services.submit_task_for_approval(
        request.auth, task, files=files, links=links, page_ids=page_ids,
    )
    if error == 'forbidden':
        return payload('Only the assignee can submit this task for approval.', 403, False)
    if error == 'invalid_status':
        return payload('Only a task that is In Progress can be submitted for approval.', 400, False)
    if error == 'already_pending':
        return payload('This task already has a pending approval request.', 400, False)
    if error == 'deadline_passed':
        return payload('This task is past its deadline and can no longer be submitted.', 400, False)
    if error == 'no_evidence':
        return payload('At least one piece of evidence (file, link, or page) is required.', 400, False)
    if error == 'too_large':
        return payload('One of the uploaded files exceeds the maximum allowed size (10MB).', 400, False)
    if error == 'invalid_content_type':
        return payload('One of the uploaded files has a disallowed type.', 400, False)
    if error == 'invalid_page':
        return payload('One or more pages are invalid for this company.', 400, False)
    return payload(
        'Task submitted for approval.', 202, True, {'approval': await _approval_data(approval, request.auth)},
    )


@router.post(
    '/tasks/{task_id}/approve/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def approve_task(request, task_id: UUID):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    updated, error = await services.approve_task(request.auth, task)
    if error == 'forbidden':
        return payload('You do not have permission to approve this task.', 403, False)
    if error == 'no_pending_approval':
        return payload('This task has no pending approval to review.', 400, False)
    return payload('Task approved.', 200, True, {'task': await task_data(updated)})


@router.post(
    '/tasks/{task_id}/reject/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def reject_task(request, task_id: UUID, data: TaskRejectIn):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    updated, error = await services.reject_task_approval(request.auth, task, data.comment)
    if error == 'forbidden':
        return payload('You do not have permission to review this task.', 403, False)
    if error == 'comment_required':
        return payload('A rejection comment is required.', 400, False)
    if error == 'no_pending_approval':
        return payload('This task has no pending approval to review.', 400, False)
    return payload('Task submission rejected.', 200, True, {'task': await task_data(updated)})


@router.get(
    '/tasks/{task_id}/approvals/', auth=auth,
    response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def list_task_approvals(request, task_id: UUID):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    approvals = [
        a async for a in task.approvals.select_related('submitted_by', 'decided_by').order_by('-submitted_at')
    ]
    return payload('Approvals retrieved successfully.', 200, True, {
        'results': [await _approval_data(a, request.auth) for a in approvals],
    })


@router.post(
    '/tasks/{task_id}/extend-deadline/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def extend_task_deadline(request, task_id: UUID, data: DeadlineExtendIn):
    task, error = await services.get_task_for_user(request.auth, task_id)
    if error == 'not_found':
        return payload('Task not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this task.', 403, False)
    updated, error = await services.extend_task_deadline(request.auth, task, data.deadline)
    if error == 'forbidden':
        return payload('Only the project creator can extend a task deadline.', 403, False)
    if error == 'not_an_extension':
        return payload('The new deadline must be later than the current one.', 400, False)
    if error == 'exceeds_project_deadline':
        return payload("The task's deadline must remain before the project's deadline.", 400, False)
    return payload('Task deadline extended.', 200, True, {'task': await task_data(updated)})


# --------------------------------------------------------------------------
# Time logs -- one real, attributable entry per unit of work logged against
# a task. Independent of the approval workflow above: logging time tracks
# effort spent, not task completion.
# --------------------------------------------------------------------------

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
