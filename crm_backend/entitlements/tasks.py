"""Nightly reconciliation of usage counters."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def reconcile_usage_counters_task():
    """Recompute every point-in-time counter from the rows it summarises.

    Counters are for speed; rows are for truth. The decision path already
    reads rows directly (see entitlements.services.current_usage_sync), so a
    drifted counter cannot wrongly allow or refuse anything -- it can only
    make a dashboard wrong. This keeps the dashboards honest and, more
    usefully, makes drift *visible*: every correction is logged, and a counter
    that needs correcting regularly means a create or delete path is not
    recording usage.
    """
    from company.models import Company

    from .services import reconcile_company_sync

    corrected = 0
    for company in Company.objects.all().iterator():
        for change in reconcile_company_sync(company):
            corrected += 1
            logger.warning(
                'usage_counter.drift_corrected company=%s metric=%s was=%s now=%s',
                company.id, change['metric'], change['was'], change['now'],
            )
    return corrected
