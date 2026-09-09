"""The one way to write an audit row.

Every caller goes through :func:`record_event`. Nothing constructs an
``AuditEvent`` inline. That is not style preference: it is the seam that lets a
real domain-event bus take over later -- publish from here, keep writing the
row, and no call site changes. The bus itself is not being built now.
"""

import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from asgiref.sync import sync_to_async
from utils.middleware import get_current_request_id

from .models import AuditAction, AuditEvent

logger = logging.getLogger(__name__)

__all__ = ['AuditAction', 'record_event', 'arecord_event']


def _json_safe(value):
    """Coerce a value into something ``JSONField`` can store and a human can
    still read three years from now.

    Deliberately lossy in one direction only: UUIDs, datetimes and durations
    become strings rather than being dropped, so a row never silently loses the
    detail it was written to capture.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    # A model instance, or anything else: keep its identity, not its state. A
    # full object dump is how audit tables end up holding copies of data that
    # was later deleted for a reason.
    primary_key = getattr(value, 'pk', None)
    if primary_key is not None:
        return str(primary_key)
    return str(value)


def record_event(*, company, actor, action, target, before=None, after=None, reason='', request_id=None):
    """Write one audit row and return it.

    ``target`` is the model instance the action happened to; its
    ``app_label.modelname`` and pk are stored, not a foreign key, so the row
    survives the target being deleted.

    ``before``/``after`` should carry only the fields the action changed.

    ``request_id`` defaults to the current request's correlation id
    (``utils.middleware``), so callers never pass it and log lines and audit
    rows can be lined up after the fact.

    Failures propagate. The tempting alternative -- catch, log, return None, so
    a broken audit write never fails a business operation -- does not actually
    work: callers record inside the transaction that performed the change, and
    once a statement errors there, Postgres refuses every subsequent statement
    in that transaction anyway. So the choice is not "lose the row or lose the
    operation", it is "fail loudly or fail confusingly". Callers that genuinely
    can tolerate a missing row should say so explicitly at their own call site.
    """
    event = AuditEvent.objects.create(
        company=company,
        actor=actor,
        action=action,
        target_type=target._meta.label_lower,
        target_id=target.pk,
        before=_json_safe(before),
        after=_json_safe(after),
        reason=reason or '',
        request_id=request_id if request_id is not None else get_current_request_id(),
    )
    logger.info(
        'audit action=%s target=%s:%s company=%s actor=%s',
        action, event.target_type, event.target_id, company.pk, getattr(actor, 'pk', None),
    )
    return event


arecord_event = sync_to_async(record_event, thread_sensitive=True)
arecord_event.__doc__ = 'Async wrapper over :func:`record_event`, for the async service layer.'
