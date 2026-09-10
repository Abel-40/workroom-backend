"""The AI-assisted brief route, and its two human gates (§1).

The shape §1 asks for, in order::

    upload a doc
      -> AI extracts into the form
      -> HUMAN CONFIRMS            (gate 1: writes ProjectBrief)
      -> AI interprets the brief
      -> HUMAN CONFIRMS            (gate 2: marks the assist READY)
      -> only now may the full decomposition run

Two gates, not one. The value is in the second: a model can extract a brief
faithfully and still have misunderstood what the project is *for*, and the
cheapest possible moment to find that out is before a full plan is generated
on top of it.

Nothing here lets the AI write project state. Gate 1 applies the extraction
through ``projects_and_tasks.services.update_brief`` -- the same validated
path the guided form uses -- so an extraction nobody read cannot become a
brief, and the reviewer's own edits win over what the model produced.
"""

import logging

from asgiref.sync import sync_to_async
from django.utils import timezone
from projects_and_tasks.services import update_brief, user_can_manage_project

from .models import BriefAssist
from .tasks_brief import mark_ready, process_brief_extraction, process_brief_interpretation

logger = logging.getLogger(__name__)

# The prose fields an extraction may fill. Named here as well as in the AI
# service's schema, on purpose: this is the boundary that decides what may be
# written, and it must not be inferable from whatever the model happened to
# return.
EXTRACTABLE_FIELDS = (
    'objective', 'background', 'scope_in', 'scope_out', 'expected_outcome', 'constraints',
)

# A document longer than this is refused rather than silently truncated --
# a brief extracted from the first fifth of a specification is a brief nobody
# can trust, and the person uploading it would have no way to know.
MAX_SOURCE_CHARS = 60_000


async def start_extraction(user, project, *, source_text: str, source_filename: str = ''):
    """Returns (assist, error) with error 'forbidden', 'empty_source',
    'source_too_large', 'already_in_flight', or None.

    Requires MANAGE: this ends in writing the project's brief, which is a
    management act, and the confirmation gate does not change that.
    """
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'

    text = (source_text or '').strip()
    if not text:
        return None, 'empty_source'
    if len(text) > MAX_SOURCE_CHARS:
        return None, 'source_too_large'

    in_flight = await BriefAssist.objects.filter(
        project=project,
        status__in=[BriefAssist.STATUS.EXTRACTING, BriefAssist.STATUS.INTERPRETING],
    ).afirst()
    if in_flight is not None:
        # The same guard every other AI operation here has: a double-click
        # must not buy two provider calls.
        return in_flight, 'already_in_flight'

    assist = await BriefAssist.objects.acreate(
        project=project, requested_by=user, source_filename=source_filename[:255],
        source_chars=len(text), status=BriefAssist.STATUS.EXTRACTING,
    )
    await sync_to_async(process_brief_extraction.delay, thread_sensitive=True)(str(assist.id), text)
    return assist, None


async def get_assist_for_user(user, project, assist_id=None):
    """The named assist, or this project's most recent one. Returns
    (assist, error) with error 'not_found' or None. Scoped to the project the
    caller already reached, so an id from another project is a 404."""
    queryset = BriefAssist.objects.filter(project=project)
    assist = await (
        queryset.filter(id=assist_id).afirst() if assist_id else queryset.order_by('-created_at').afirst()
    )
    if assist is None:
        return None, 'not_found'
    return assist, None


async def confirm_extraction(user, project, assist, *, overrides=None):
    """**Gate 1.** Write the confirmed brief, then start the interpretation.

    ``overrides`` are the reviewer's edits, and they win. That is the whole
    reason the gate exists: what gets written is what a person agreed to,
    which may be nothing the model said.

    Returns (assist, error) with error 'forbidden', 'wrong_state', or whatever
    ``update_brief`` returned.
    """
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'
    if assist.status != BriefAssist.STATUS.EXTRACTED:
        return None, 'wrong_state'

    extracted = assist.extracted or {}
    updates = {field: extracted.get(field, '') or '' for field in EXTRACTABLE_FIELDS}
    for field, value in (overrides or {}).items():
        if field in EXTRACTABLE_FIELDS and value is not None:
            updates[field] = value

    # Through the same validated path the guided form uses -- never a direct
    # write from stored JSON.
    _, error = await update_brief(user, project, updates)
    if error:
        return None, error

    assist.extraction_confirmed_at = timezone.now()
    assist.status = BriefAssist.STATUS.INTERPRETING
    await assist.asave(update_fields=['extraction_confirmed_at', 'status', 'updated_at'])
    await sync_to_async(process_brief_interpretation.delay, thread_sensitive=True)(str(assist.id))
    logger.info('brief_assist.extraction_confirmed', extra={'assist_id': str(assist.id)})
    return assist, None


async def confirm_interpretation(user, project, assist):
    """**Gate 2.** The last thing standing between a brief and a plan.

    Returns (assist, error) with error 'forbidden', 'wrong_state', or None.
    """
    if not await user_can_manage_project(user, project):
        return None, 'forbidden'
    if assist.status != BriefAssist.STATUS.INTERPRETED:
        return None, 'wrong_state'
    await sync_to_async(mark_ready, thread_sensitive=True)(assist)
    logger.info('brief_assist.interpretation_confirmed', extra={'assist_id': str(assist.id)})
    return assist, None


async def assist_blocks_planning(project) -> bool:
    """Whether an AI-assisted brief is part-way through its gates.

    §1's "then and only then" applies to the assisted route: a project whose
    extraction or interpretation is still awaiting confirmation must not be
    planned from, because the brief a plan would be built on is one nobody has
    agreed to yet.

    A project with **no** assist at all is not blocked -- the guided form is
    still the default way in, and it needs no gates because a person wrote
    every word of it themselves.
    """
    latest = await BriefAssist.objects.filter(project=project).order_by('-created_at').afirst()
    if latest is None:
        return False
    return latest.status in (
        BriefAssist.STATUS.EXTRACTING,
        BriefAssist.STATUS.EXTRACTED,
        BriefAssist.STATUS.INTERPRETING,
        BriefAssist.STATUS.INTERPRETED,
    )
