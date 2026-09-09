"""Usage counters.

Counters are for speed; rows are for truth. Every counter here is derivable
from the tables it summarises, and a nightly job recomputes them and corrects
drift (see :mod:`entitlements.tasks`). Nothing may treat a counter as
authoritative for anything a user would notice being wrong.
"""

from django.db import models
from utils.models import UUIDModel


class UsageCounter(UUIDModel):
    """One (company, metric, period) tally.

    ``period`` is a month key like ``"2026-09"`` for metrics that reset, and
    the empty string for point-in-time ones. Keeping both shapes in one table
    rather than splitting them means the reconciliation job, the read path and
    the admin all have one place to look; the cost is that ``period`` is
    meaningless for half the rows, which the metric name already tells you.

    Monthly metrics reset by the period key rather than by a scheduled wipe:
    a new month is simply a new row starting at zero. There is no job to fail
    at midnight on the first, and last month's number stays readable.
    """

    class Metric(models.TextChoices):
        # Point-in-time: incremented and decremented as things are created and
        # removed.
        MEMBERS = 'members', 'Members'
        ACTIVE_PROJECTS = 'active_projects', 'Active projects'
        DEPARTMENTS = 'departments', 'Departments'
        TEAMS = 'teams', 'Teams'
        STORAGE_BYTES = 'storage_bytes', 'Storage bytes'
        INFO_PORTAL_PAGES = 'info_portal_pages', 'Info Portal pages'
        INTEGRATIONS = 'integrations', 'Integrations'
        # Monthly: only ever incremented, and reset by the period key.
        AI_CREDITS = 'ai_credits', 'AI credits'
        AI_PLANNING_GENERATIONS = 'ai_planning_generations', 'AI planning generations'

    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='usage_counters')
    metric = models.CharField(max_length=40, choices=Metric.choices)
    period = models.CharField(max_length=7, blank=True, default='')
    # BigInteger because storage_bytes shares this column with counts in the
    # single digits. A company on the 1 TB plan overflows a 32-bit integer.
    value = models.BigIntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'metric', 'period'],
                name='one_usage_counter_per_company_metric_period',
            ),
        ]
        indexes = [
            models.Index(fields=['company', 'metric', 'period']),
        ]

    def __str__(self):
        return f'{self.company_id} {self.metric} {self.period or "-"} = {self.value}'
