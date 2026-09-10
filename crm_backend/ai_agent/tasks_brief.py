"""Celery tasks for the AI-assisted brief route (§1).

Two calls, each followed by a human confirmation:

``process_brief_extraction``
    Reads an uploaded document's text and fills the brief form. Writes only to
    ``BriefAssist.extracted`` -- never to ``ProjectBrief``. The person doing
    the confirming is the one who decides what, if anything, becomes project
    state.

``process_brief_interpretation``
    Reads the *confirmed* brief back and says what it understood. Cheap by
    design, and deliberately not a plan.

Same retry posture as every other AI task here: transient failures retry three
times, invalid structured output fails outright rather than looping over a
response that will never become valid.

Deliberately its own module rather than merged into tasks.py, matching
tasks_health.py / tasks_todos.py / tasks_assistant.py.
"""

import logging

import requests
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .context import estimate_cost
from .models import BriefAssist

logger = logging.getLogger(__name__)


class TransientBriefServiceError(Exception):
    """Network error, timeout, or 5xx/429 -- safe to retry."""


class PermanentBriefServiceError(Exception):
    """The AI service rejected the request or returned invalid output."""


def _post(path: str, payload: dict) -> dict:
    headers = {}
    if settings.WORKROOM_AI_SERVICE_TOKEN:
        headers['X-Service-Token'] = settings.WORKROOM_AI_SERVICE_TOKEN
    try:
        response = requests.post(
            f'{settings.WORKROOM_AI_SERVICE_URL}{path}',
            json=payload, headers=headers, timeout=settings.WORKROOM_AI_SERVICE_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TransientBriefServiceError(str(exc)) from exc

    if response.status_code == 200:
        return response.json()
    if response.status_code in (429, 503) or response.status_code >= 500:
        raise TransientBriefServiceError(f'AI service returned {response.status_code}')
    raise PermanentBriefServiceError(
        f'AI service rejected the request ({response.status_code}): {response.text[:500]}'
    )


def _record_usage(assist: BriefAssist, data: dict):
    """Usage is recorded per step and accumulated, because a single assist can
    make two paid calls and the row should end up holding what the whole thing
    cost, not what its last step did."""
    usage = data.get('usage') or {}
    provider = (data.get('provider') or '')[:50]
    model = (data.get('model') or '')[:100]
    input_tokens = usage.get('input_tokens')
    output_tokens = usage.get('output_tokens')

    assist.provider = provider or assist.provider
    assist.model = model or assist.model
    if input_tokens is not None:
        assist.input_tokens = (assist.input_tokens or 0) + int(input_tokens)
    if output_tokens is not None:
        assist.output_tokens = (assist.output_tokens or 0) + int(output_tokens)

    step_cost = estimate_cost(provider, model, input_tokens, output_tokens)
    if step_cost is not None:
        assist.cost = (assist.cost or 0) + step_cost


def _fail(assist: BriefAssist, message: str):
    assist.status = BriefAssist.STATUS.FAILED
    assist.error_message = message[:2000]
    assist.save(update_fields=['status', 'error_message', 'updated_at'])


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_brief_extraction(self, assist_id: str, source_text: str):
    """``source_text`` is passed as an argument rather than stored on the row:
    the document's contents are a means to the brief, not something this
    feature should quietly become a store of. It lives only as long as the
    task that reads it."""
    assist = BriefAssist.objects.filter(id=assist_id).select_related('project').first()
    if assist is None:
        logger.warning('brief_assist.not_found', extra={'assist_id': str(assist_id)})
        return
    if assist.status != BriefAssist.STATUS.EXTRACTING:
        # A repeated delivery of the same task. Retries must be idempotent.
        logger.info('brief_assist.extract_skipped', extra={'assist_id': str(assist_id)})
        return

    payload = {
        'assist_id': str(assist.id),
        'project_title': assist.project.title,
        'source_text': source_text,
    }
    try:
        body = _post('/api/v1/brief-extract', payload)
    except TransientBriefServiceError as exc:
        if self.request.retries >= self.max_retries:
            _fail(assist, f'AI service unavailable after {self.max_retries} retries: {exc}')
            return
        raise self.retry(exc=exc)
    except PermanentBriefServiceError as exc:
        _fail(assist, str(exc))
        return

    data = body.get('data') or {}
    _record_usage(assist, data)
    assist.extracted = {
        field: data.get(field, '') or ''
        for field in ('objective', 'background', 'scope_in', 'scope_out', 'expected_outcome', 'constraints')
    }
    assist.extracted['missing_fields'] = data.get('missing_fields') or []
    assist.status = BriefAssist.STATUS.EXTRACTED
    assist.save()
    logger.info('brief_assist.extracted', extra={'assist_id': str(assist.id)})


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_brief_interpretation(self, assist_id: str):
    """Reads the project's brief *as confirmed*, not the raw extraction.

    That distinction is the point of gate 1: whatever the person edited and
    accepted is what gets interpreted, so the second gate is answering for the
    text that will actually be planned from.
    """
    assist = BriefAssist.objects.filter(id=assist_id).select_related('project').first()
    if assist is None:
        logger.warning('brief_assist.not_found', extra={'assist_id': str(assist_id)})
        return
    if assist.status != BriefAssist.STATUS.INTERPRETING:
        logger.info('brief_assist.interpret_skipped', extra={'assist_id': str(assist_id)})
        return

    brief = getattr(assist.project, 'brief', None)
    payload = {
        'assist_id': str(assist.id),
        'project_title': assist.project.title,
        **{
            field: (getattr(brief, field, '') or '') if brief else ''
            for field in ('objective', 'background', 'scope_in', 'scope_out', 'expected_outcome', 'constraints')
        },
    }
    try:
        body = _post('/api/v1/brief-interpret', payload)
    except TransientBriefServiceError as exc:
        if self.request.retries >= self.max_retries:
            _fail(assist, f'AI service unavailable after {self.max_retries} retries: {exc}')
            return
        raise self.retry(exc=exc)
    except PermanentBriefServiceError as exc:
        _fail(assist, str(exc))
        return

    data = body.get('data') or {}
    _record_usage(assist, data)
    assist.interpretation = {
        'restated_objective': data.get('restated_objective', ''),
        'assumptions': data.get('assumptions') or [],
        'open_questions': data.get('open_questions') or [],
    }
    assist.status = BriefAssist.STATUS.INTERPRETED
    assist.save()
    logger.info('brief_assist.interpreted', extra={'assist_id': str(assist.id)})


def mark_ready(assist: BriefAssist):
    """Gate 2 passed. Recorded as a state rather than inferred from the
    presence of an interpretation, so "a person agreed to this" is a fact the
    row holds rather than a guess a reader makes."""
    assist.status = BriefAssist.STATUS.READY
    assist.interpretation_confirmed_at = timezone.now()
    assist.save(update_fields=['status', 'interpretation_confirmed_at', 'updated_at'])
