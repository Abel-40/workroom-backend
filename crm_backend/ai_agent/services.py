"""AI generation lifecycle (Phase 5): create a traceable PENDING record and
hand off to Celery. This module never talks to an LLM or the FastAPI AI
service directly -- that boundary is Phase 6/7 (ARCHITECTURE.md Section 4/7).
"""

import logging

from asgiref.sync import sync_to_async
from company.services import is_company_member
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone
from projects_and_tasks.services import is_eligible_assignee, user_can_manage_project, user_can_view_project

from .brief_services import assist_blocks_planning
from .context import build_assignee_refs
from .models import AIGeneration
from .tasks import process_ai_generation

logger = logging.getLogger(__name__)

User = get_user_model()


async def find_in_flight_plan(project):
    """A plan generation already running for this project.

    The same guard ``todos.services.find_in_flight_generation`` gives to-do
    generation, generalised here per §4: a double-click must not buy two
    provider calls. Scoped to the project rather than the user, because two
    managers asking at the same moment is the same waste as one manager
    asking twice.
    """
    return await AIGeneration.objects.filter(
        project=project, status__in=[AIGeneration.STATUS.PENDING, AIGeneration.STATUS.PROCESSING],
    ).afirst()


async def find_by_idempotency_key(project, idempotency_key):
    """The generation a repeated request is really asking about.

    Scoped to the project so a key cannot reach across tenants, and matched
    regardless of status: replaying a key must return the original outcome,
    including a failure. Returning a fresh generation for a retried key is how
    one user action becomes two bills.
    """
    if not idempotency_key:
        return None
    return await AIGeneration.objects.filter(
        project=project, idempotency_key=idempotency_key,
    ).order_by('-requested_at').afirst()


async def request_project_plan(
    user, project, *, prompt: str, mentioned_user_ids: list | None = None,
    assignee_ids: list | None = None, max_tasks: int = 10, idempotency_key: str = '',
):
    """Returns (generation, error).

    ``error`` is 'forbidden', 'plan_already_saved', 'invalid_assignee',
    'queue_failed', or None. Two non-errors deserve mention: a repeated
    ``idempotency_key`` and an in-flight generation both return the *existing*
    generation with no error, because the caller asked for a plan and there is
    one -- they did not ask for a second one.
    """
    if not await user_can_view_project(user, project):
        return None, 'forbidden'
    if await AIGeneration.objects.filter(project=project, saved_at__isnull=False).aexists():
        return None, 'plan_already_saved'
    # §1's "then and only then": a brief that is part-way through the assisted
    # route has not been confirmed by anyone yet, and planning from it would
    # skip the gate that exists to catch a misunderstanding while it is still
    # cheap. A project with no assist at all is unaffected -- the guided form
    # needs no gate, because a person wrote every word of it.
    if await assist_blocks_planning(project):
        return None, 'brief_not_confirmed'

    existing = await find_by_idempotency_key(project, idempotency_key)
    if existing is not None:
        return existing, None
    in_flight = await find_in_flight_plan(project)
    if in_flight is not None:
        return in_flight, None

    requirements = prompt
    if mentioned_user_ids:
        # Informational only -- names the requester referenced while
        # describing the work, never a structured "assign to" instruction.
        # The AI has no assignee field to write to (see ai_schemas.GeneratedTask);
        # real assignment happens explicitly in the review step.
        candidates = [user async for user in User.objects.filter(id__in=mentioned_user_ids)]
        mentioned_names = [
            (candidate.first_name or candidate.username)
            for candidate in candidates if await is_company_member(candidate, project.company)
        ]
        if mentioned_names:
            requirements += f"\n\nTeam members mentioned by the requester (for context only, do not assign tasks): {', '.join(mentioned_names)}"

    # This is a different thing from mentioned_user_ids above: a human-
    # approved pool the AI may *suggest* per task (never assign outright --
    # Rule 10). Every id must already be eligible for this project or the
    # whole request is rejected (fail closed, Rule 4).
    assignee_ids = assignee_ids or []
    for assignee_id in assignee_ids:
        candidate = await User.objects.filter(id=assignee_id).afirst()
        if candidate is None or not await is_eligible_assignee(user, project, candidate):
            return None, 'invalid_assignee'

    try:
        generation = await AIGeneration.objects.acreate(
            project=project, requested_by=user, prompt=requirements,
            requested_assignee_ids=[str(assignee_id) for assignee_id in assignee_ids], max_tasks=max_tasks,
            # Minted once, at creation, from exactly this generation's
            # approved pool -- see ai_agent.context. The AI never sees a real
            # user id.
            assignee_refs=build_assignee_refs(assignee_ids),
            idempotency_key=idempotency_key or '',
        )
    except IntegrityError:
        # Lost a race against a second request carrying the same key -- the
        # database-level twin of the check above, for the window between it
        # and this insert. The other request's row is the answer.
        winner = await find_by_idempotency_key(project, idempotency_key)
        if winner is not None:
            return winner, None
        raise
    try:
        # thread_sensitive=True: under CELERY_TASK_ALWAYS_EAGER (tests),
        # .delay() runs the task body inline on whatever thread this runs
        # on -- it must be the same thread/connection as the just-created,
        # not-yet-committed generation row, or the task's own query for it
        # returns nothing. See api/api.py::send_invite for the same fix.
        await sync_to_async(process_ai_generation.delay, thread_sensitive=True)(str(generation.id))
    except Exception:
        generation.status = AIGeneration.STATUS.FAILED
        generation.error_message = 'Failed to queue the AI generation job.'
        await generation.asave(update_fields=['status', 'error_message'])
        logger.exception('ai_generation.enqueue_failed', extra={'generation_id': str(generation.id)})
        return generation, 'queue_failed'
    return generation, None


async def get_generation_for_user(user, generation_id):
    generation = await AIGeneration.objects.select_related('project', 'project__company').filter(
        id=generation_id,
    ).afirst()
    if generation is None:
        return None, 'not_found'
    if not await user_can_view_project(user, generation.project):
        return None, 'forbidden'
    return generation, None


async def discard_generation(user, generation):
    """Abandon an unsaved draft generation. Fails closed on anything already
    saved to the backlog -- a saved plan is real project state, not a draft,
    and discarding it is not what "New plan" means."""
    if not (generation.requested_by_id == user.id or await user_can_manage_project(user, generation.project)):
        return 'forbidden'
    if generation.saved_at is not None:
        return 'already_saved'
    if generation.discarded_at is None:
        generation.discarded_at = timezone.now()
        await generation.asave(update_fields=['discarded_at'])
    return None
