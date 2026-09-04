"""Personal to-do domain logic.

Two rules drive everything here:

1. **Ownership is the only access boundary.** Every read and write is
   filtered by ``user=<the authenticated caller>``. No role, not even
   company Owner, widens that -- so unlike projects/pages there is no
   ``user_can_view_*`` helper to consult, and a client-supplied owner id is
   never accepted anywhere.
2. **"Today" is the owner's today.** A todo's due_date is a calendar day, so
   it can only be compared against a day computed in the owner's own
   timezone (users.models.User.timezone). Using the server's date would put
   someone in Addis on the wrong day for several hours of every day, and
   would do it silently.
"""

from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Count, Q
from django.utils import timezone
from projects_and_tasks.models import Task

from .models import TodoItem

# A due date this far out is far likelier to be a typo (or a bad AI
# suggestion) than a real plan. Rejected rather than silently clamped, so the
# caller finds out.
MAX_FUTURE_DAYS = 365 * 5


def user_today(user) -> date:
    """The current calendar day in ``user``'s own timezone."""
    try:
        tz = ZoneInfo(user.timezone or 'UTC')
    except (ZoneInfoNotFoundError, ValueError):
        # A stored timezone can only get invalid through data drift (the
        # value is validated on write in users.services). Fall back rather
        # than 500 on every todo request for that user.
        tz = ZoneInfo('UTC')
    return timezone.now().astimezone(tz).date()


def validate_due_date(user, due_date: date) -> str | None:
    """Returns an error code, or None when the date is acceptable. Past dates
    are allowed on purpose -- an overdue todo is a real, useful state, and
    backfilling yesterday is legitimate."""
    if due_date is None:
        return 'due_date_required'
    if due_date > user_today(user) + timedelta(days=MAX_FUTURE_DAYS):
        return 'due_date_too_far'
    return None


# --------------------------------------------------------------------------
# Task linking
# --------------------------------------------------------------------------

async def get_assignable_task(user, task_id):
    """Resolves a task the caller may attach a todo to.

    Assignment, not visibility, is the test: a user can *see* plenty of
    company-visible tasks they have nothing to do with, and the product rule
    is that todos may only be built from work actually assigned to them.
    Returns (task, error) with error in {'not_found', 'not_assigned'}.
    """
    task = await Task.objects.select_related('project', 'project__company').filter(
        id=task_id, is_deleted=False,
    ).afirst()
    if task is None:
        return None, 'not_found'
    if task.assigned_to_id != user.id:
        # Deliberately collapsed into the same client-visible response as
        # 'not found': a non-assignee must not be able to probe which task
        # ids exist.
        return None, 'not_assigned'
    return task, None


def list_assigned_tasks(user, company, *, open_only: bool = True):
    """The caller's own assigned tasks -- the picker behind "make todos for
    this task", and the source set for an AI generation. Never widened to
    anything the user merely has permission to view. Returns an unevaluated
    queryset."""
    queryset = Task.objects.select_related('project').filter(
        assigned_to=user, is_deleted=False, project__company=company, project__is_deleted=False,
    )
    if open_only:
        queryset = queryset.exclude(status=Task.STATUS.DONE)
    return queryset.order_by('deadline', 'created_at')


def task_link_is_live(todo, user) -> bool:
    """Whether the todo's task link should still be exposed.

    A task can be reassigned away from the todo's owner after the todo was
    made. When that happens the owner keeps their private note (and its
    snapshotted title) but loses the live link -- continuing to serve the
    task's current state would leak work they are no longer part of.
    """
    return todo.task_id is not None and todo.task is not None and todo.task.assigned_to_id == user.id


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------

def _base_queryset(user):
    return TodoItem.objects.select_related('task').filter(user=user, is_deleted=False)


def list_todos(user, *, scope: str = 'all', include_done: bool = False,
               due_from: date | None = None, due_to: date | None = None):
    """Nearest-first list of the caller's own todos.

    Returns an unevaluated queryset for the caller to paginate, matching the
    convention in pages/services.py and api/routers/documents.py. Model Meta
    ordering (due_date, position, created_at) already delivers "nearest
    first" and floats overdue items to the top.
    """
    queryset = _base_queryset(user)
    today = user_today(user)

    if scope == 'overdue':
        queryset = queryset.filter(due_date__lt=today, is_done=False)
    elif scope == 'today':
        queryset = queryset.filter(due_date=today)
    elif scope == 'upcoming':
        queryset = queryset.filter(due_date__gt=today)
    elif scope == 'due':
        # Everything actually demanding attention right now: overdue plus
        # today, which is what the default screen leads with.
        queryset = queryset.filter(due_date__lte=today)

    if due_from is not None:
        queryset = queryset.filter(due_date__gte=due_from)
    if due_to is not None:
        queryset = queryset.filter(due_date__lte=due_to)
    if not include_done:
        queryset = queryset.filter(is_done=False)
    return queryset


async def get_todo_for_user(user, todo_id):
    """Returns (todo, error) with error 'not_found' only.

    There is no 'forbidden' case by design: someone else's todo is not a
    thing the caller may learn exists, so a foreign id is indistinguishable
    from a made-up one.
    """
    todo = await _base_queryset(user).filter(id=todo_id).afirst()
    if todo is None:
        return None, 'not_found'
    return todo, None


async def summarize(user) -> dict:
    """Counts for the sidebar badge and the list header. One aggregate query
    rather than three round trips."""
    today = user_today(user)
    counts = await _base_queryset(user).filter(is_done=False).aaggregate(
        overdue=Count('id', filter=Q(due_date__lt=today)),
        # Named due_today, not today: the response also carries the caller's
        # actual calendar date under 'today', and a count sharing that key
        # would be silently overwritten by it.
        due_today=Count('id', filter=Q(due_date=today)),
        upcoming=Count('id', filter=Q(due_date__gt=today)),
    )
    counts['open'] = counts['overdue'] + counts['due_today'] + counts['upcoming']
    return counts


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------

async def _next_position(user, due_date: date) -> int:
    """Append to the end of that day's list. Positions are per (user, day)
    and only ever compared within one day, so gaps left by deletions are
    harmless."""
    last = await _base_queryset(user).filter(due_date=due_date).order_by('-position').afirst()
    return (last.position + 1) if last else 0


async def create_todo(user, company, *, title: str, due_date: date, notes: str = '',
                      task=None, source: str = TodoItem.SOURCE.MANUAL):
    error = validate_due_date(user, due_date)
    if error:
        return None, error
    todo = await TodoItem.objects.acreate(
        user=user, company=company, title=title.strip(), notes=notes.strip(),
        due_date=due_date, position=await _next_position(user, due_date),
        task=task, task_title_snapshot=(task.title if task else ''), source=source,
    )
    return todo, None


async def update_todo(user, todo, updates: dict):
    """Only ever writes the fields a user is allowed to change. `user`,
    `company`, `task` and `source` are deliberately absent: re-pointing a
    todo at a different task would bypass the assignment check done at
    creation, so a re-link is a delete plus a create."""
    fields = []
    if updates.get('title') is not None:
        todo.title = updates['title'].strip()
        fields.append('title')
    if updates.get('notes') is not None:
        todo.notes = updates['notes'].strip()
        fields.append('notes')
    if updates.get('due_date') is not None:
        error = validate_due_date(user, updates['due_date'])
        if error:
            return None, error
        if updates['due_date'] != todo.due_date:
            todo.due_date = updates['due_date']
            # Moving to another day makes the old position meaningless.
            todo.position = await _next_position(user, todo.due_date)
            fields.extend(['due_date', 'position'])
    if updates.get('position') is not None:
        todo.position = updates['position']
        fields.append('position')
    if updates.get('is_done') is not None:
        done = bool(updates['is_done'])
        if done != todo.is_done:
            todo.is_done = done
            todo.completed_at = timezone.now() if done else None
            fields.extend(['is_done', 'completed_at'])
    if not fields:
        return todo, None
    fields.append('updated_at')
    await todo.asave(update_fields=fields)
    return todo, None


async def delete_todo(user, todo) -> bool:
    """Soft delete, matching every other domain object in this codebase."""
    todo.is_deleted = True
    await todo.asave(update_fields=['is_deleted', 'updated_at'])
    return True
