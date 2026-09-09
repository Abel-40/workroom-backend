"""Plan limits: resolving them, counting against them, enforcing them.

**Every limit check in the codebase goes through this module.** If you find
yourself writing ``if company.plan == "free"`` anywhere else, that is the bug
this module exists to prevent -- the rule lives here, once, or it drifts.

The enforcement switch
----------------------
``settings.ENTITLEMENTS_ENFORCED`` decides whether an over-limit result
actually blocks. It exists because the source documents disagree:

- §11 of the V2 prompt is titled "real limits, real enforcement" and says, in
  bold, "not a document, not a permissive stub".
- The same prompt's OUT OF SCOPE list says "entitlement *enforcement* (limits
  must stay permissive)".
- `CLAUDE.md` §15 excludes an "advanced subscriptions/billing product" from
  V1, and §16 says "do not expand billing into a V1 product feature".

Two of the three say do not enforce. The machinery is built either way --
resolving, counting and reconciling are useful on their own, and they are the
part that cannot be reconstructed retroactively. What the flag changes is only
the last step: whether ``check()`` returns ``allowed=False``.

When it is off, every check still runs, still counts, and still reports the
real numbers; it simply never refuses. That means turning enforcement on later
is a settings change, not a code change, and the usage data to decide it with
already exists. See R1 in `docs/BUILD_LOG.md`.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from .models import UsageCounter

# How long a past_due company keeps full access before anything gated by
# check() goes read-only. §11: fourteen days.
GRACE_PERIOD_DAYS = 14

# AI operation costs, in credits. Named here rather than at each call site so
# the price list is one thing somebody can read.
COST_ASSISTANT_QUESTION = 1
COST_TODO_GENERATION = 2
COST_HEALTH_SUMMARY = 3
# Planning scales with brief size: §11 asks for 15-25 on a simple size-based
# scale, documented where it is implemented. See planning_generation_cost.
COST_PLANNING_MIN = 15
COST_PLANNING_MAX = 25
PLANNING_CHARS_PER_STEP = 2000

MONTHLY_METRICS = frozenset({
    UsageCounter.Metric.AI_CREDITS,
    UsageCounter.Metric.AI_PLANNING_GENERATIONS,
})

# The plan_snapshot key each metric reads. Explicit rather than derived from
# the metric name, because two of them differ and a silent mismatch would
# resolve every limit to "unlimited" -- which is the failure mode nobody
# notices.
METRIC_LIMIT_KEY = {
    UsageCounter.Metric.MEMBERS: 'max_members',
    UsageCounter.Metric.ACTIVE_PROJECTS: 'max_active_projects',
    UsageCounter.Metric.DEPARTMENTS: 'max_departments',
    UsageCounter.Metric.TEAMS: 'max_teams',
    UsageCounter.Metric.STORAGE_BYTES: 'storage_bytes',
    UsageCounter.Metric.INFO_PORTAL_PAGES: 'info_portal_pages',
    UsageCounter.Metric.INTEGRATIONS: 'integrations',
    UsageCounter.Metric.AI_CREDITS: 'ai_credits',
    UsageCounter.Metric.AI_PLANNING_GENERATIONS: 'ai_planning_generations',
}


@dataclass(frozen=True)
class EntitlementSet:
    """One company's resolved limits."""

    limits: dict = field(default_factory=dict)
    features: frozenset = frozenset()
    plan_key: str | None = None
    read_only: bool = False

    def limit(self, metric):
        """The numeric limit, or None for unlimited."""
        return self.limits.get(METRIC_LIMIT_KEY.get(metric, metric))


@dataclass(frozen=True)
class EntitlementResult:
    """The answer to "may this company do one more of these?"."""

    allowed: bool
    current: int
    limit: int | None
    reason: str = ''
    metric: str = ''

    @property
    def remaining(self):
        return None if self.limit is None else max(self.limit - self.current, 0)


def current_period() -> str:
    return timezone.now().strftime('%Y-%m')


def period_for(metric) -> str:
    return current_period() if metric in MONTHLY_METRICS else ''


def planning_generation_cost(brief_text: str = '') -> int:
    """Credits for one AI planning generation, scaled by brief size.

    The formula, stated plainly because §11 asks for it to be documented where
    it lives: start at COST_PLANNING_MIN, add one credit per
    PLANNING_CHARS_PER_STEP characters of brief, clamp at COST_PLANNING_MAX.
    A longer brief means a longer prompt and more output, and the cap keeps a
    pathologically long one from emptying a month's pool in a single call.
    """
    steps = len(brief_text or '') // PLANNING_CHARS_PER_STEP
    return min(COST_PLANNING_MIN + steps, COST_PLANNING_MAX)


# --------------------------------------------------------------------------
# Resolving
# --------------------------------------------------------------------------

def resolve_sync(company) -> EntitlementSet:
    """This company's limits.

    Reads ``subscription.plan_snapshot`` first and the live ``Plan`` only as a
    fallback. That order is the point of the snapshot: editing a plan's numbers
    must not change what an existing customer already has.

    A company with no subscription at all resolves to the seeded `free` plan.
    That is a real state -- every company registered before billing existed is
    in it -- and treating it as "no limits" would mean the limits apply to
    paying customers and nobody else.
    """
    from plans.models import Plan

    subscription = getattr(company, 'subscription', None)
    snapshot = getattr(subscription, 'plan_snapshot', None) or None

    if snapshot is None:
        plan = getattr(subscription, 'plan', None)
        if plan is None:
            plan = Plan.objects.filter(key=Plan.Key.FREE).first()
        snapshot = plan.as_snapshot() if plan is not None else {}

    return EntitlementSet(
        limits={k: v for k, v in snapshot.items() if k not in ('features', 'key', 'name')},
        features=frozenset(snapshot.get('features') or []),
        plan_key=snapshot.get('key'),
        read_only=_is_read_only(subscription),
    )


def _is_read_only(subscription) -> bool:
    """Whether a past_due company has run out of grace.

    Read-only means "nothing new", never "nothing readable" -- §11 is explicit
    that existing data stays fully usable and that data is never deleted for
    non-payment. So this gates `check()` and nothing else.
    """
    if subscription is None or subscription.status != 'past_due':
        return False
    since = subscription.past_due_since
    if since is None:
        # past_due with no recorded start: treat the grace period as running,
        # not as expired. Guessing against the customer on missing data is how
        # a billing bug becomes a support incident.
        return False
    return timezone.now() >= since + timedelta(days=GRACE_PERIOD_DAYS)


async def resolve(company) -> EntitlementSet:
    return await sync_to_async(resolve_sync, thread_sensitive=True)(company)


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------

def usage_sync(company, metric) -> int:
    counter = UsageCounter.objects.filter(
        company=company, metric=metric, period=period_for(metric),
    ).values_list('value', flat=True).first()
    return counter or 0


def record_usage_sync(company, metric, delta: int = 1) -> int:
    """Move a counter by ``delta``. Returns the new value.

    Uses an atomic F() update rather than read-modify-write, so two
    simultaneous uploads cannot both read 5 and both write 6. Point-in-time
    counters are clamped at zero: a counter that goes negative through some
    double-decrement would quietly grant free capacity, which is worse than
    being briefly wrong in the other direction. The nightly reconciliation
    corrects either way.
    """
    period = period_for(metric)
    with transaction.atomic():
        counter, _ = UsageCounter.objects.get_or_create(
            company=company, metric=metric, period=period,
        )
        UsageCounter.objects.filter(pk=counter.pk).update(value=F('value') + delta)
        counter.refresh_from_db(fields=['value'])
        if counter.value < 0:
            UsageCounter.objects.filter(pk=counter.pk).update(value=0)
            counter.value = 0
    return counter.value


async def record_usage(company, metric, delta: int = 1) -> int:
    return await sync_to_async(record_usage_sync, thread_sensitive=True)(company, metric, delta)


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------

def current_usage_sync(company, metric) -> int:
    """What to compare a limit against.

    **Point-in-time metrics are counted from the rows, not from the counter.**
    §11 describes incrementing a counter at each point of change and checking
    against it, which works right up until one create path forgets to
    increment -- and then the limit is wrong in whichever direction the bug
    went, silently, until somebody complains about being over or under.

    The counter for those metrics is a cache: maintained, reconciled nightly,
    and read by dashboards. The *decision* reads the rows, so it cannot drift
    from them by construction. That is the same "counters are for speed; rows
    are for truth" rule §11 states for reconciliation, applied one step
    earlier, and it deletes a whole class of bug rather than scheduling a job
    to detect it.

    Monthly metrics are different and do use the counter: credits spent leave
    no other trace, so the counter *is* the record.
    """
    if metric in MONTHLY_METRICS:
        return usage_sync(company, metric)
    actual = actual_usage_sync(company, metric)
    return usage_sync(company, metric) if actual is None else actual


def check_sync(company, metric, requested: int = 1) -> EntitlementResult:
    """May this company add ``requested`` more of ``metric``?

    Reports the true numbers whether or not enforcement is on, so the caller
    can always show "8 of 10 used" -- and so that turning enforcement on later
    does not need any caller to change.
    """
    entitlements = resolve_sync(company)
    limit = entitlements.limit(metric)
    current = current_usage_sync(company, metric)

    if entitlements.read_only:
        return EntitlementResult(
            allowed=not settings.ENTITLEMENTS_ENFORCED,
            current=current, limit=limit, metric=metric,
            reason='past_due_grace_expired',
        )
    if limit is None:
        return EntitlementResult(allowed=True, current=current, limit=None, metric=metric)
    if current + requested <= limit:
        return EntitlementResult(allowed=True, current=current, limit=limit, metric=metric)

    return EntitlementResult(
        # The single place enforcement is decided. See the module docstring.
        allowed=not settings.ENTITLEMENTS_ENFORCED,
        current=current, limit=limit, metric=metric, reason='limit_reached',
    )


async def check(company, metric, requested: int = 1) -> EntitlementResult:
    return await sync_to_async(check_sync, thread_sensitive=True)(company, metric, requested)


def has_feature_sync(company, feature: str) -> bool:
    """Whether this company's plan includes ``feature``.

    Answers True for everything when ``ENTITLEMENTS_ENFORCED`` is off, because
    the switch has to mean one thing: either plan restrictions apply or they do
    not. A build that refused to enforce a *count* but still hid a whole
    feature would be the worst of both -- permissive where it is measurable and
    restrictive where it is visible.
    """
    if not settings.ENTITLEMENTS_ENFORCED:
        return True
    return feature in resolve_sync(company).features


async def has_feature(company, feature: str) -> bool:
    return await sync_to_async(has_feature_sync, thread_sensitive=True)(company, feature)


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def actual_usage_sync(company, metric) -> int:
    """Recompute one metric from the rows it summarises.

    This is the definition of each counter. The counter itself is a cache of
    exactly this query, and the nightly job asserts they agree.
    """
    from departments_and_teams.models import Department, Team
    from documents.models import Document
    from pages.models import Page
    from projects_and_tasks.models import Project
    from users.models import CompanyUserProfile

    M = UsageCounter.Metric
    if metric == M.MEMBERS:
        return CompanyUserProfile.objects.filter(company=company, is_active=True).count()
    if metric == M.ACTIVE_PROJECTS:
        # Active only. §11: archived and done projects never block a new one.
        return Project.objects.filter(
            company=company, is_deleted=False, status=Project.STATUS.ACTIVE,
        ).count()
    if metric == M.DEPARTMENTS:
        return Department.objects.filter(company=company).count()
    if metric == M.TEAMS:
        return Team.objects.filter(company=company).count()
    if metric == M.STORAGE_BYTES:
        total = Document.objects.filter(company=company, is_deleted=False).aggregate(t=Sum('size'))
        return total['t'] or 0
    if metric == M.INFO_PORTAL_PAGES:
        return Page.objects.filter(folder__company=company, is_deleted=False).count()
    if metric == M.INTEGRATIONS:
        from integrations.models import Integration

        return Integration.objects.filter(company=company, is_active=True).count()
    # Monthly metrics are not derivable from current rows -- they record what
    # was spent, and the spending leaves no other trace. Nothing to reconcile.
    return None


def reconcile_company_sync(company) -> list[dict]:
    """Recompute every point-in-time counter and correct drift.

    Returns the corrections made, so the caller can log them. Corrections are
    expected to be rare and each one is worth seeing: a counter that drifts
    regularly means a create or delete path is not recording usage.
    """
    corrections = []
    for metric in UsageCounter.Metric.values:
        if metric in MONTHLY_METRICS:
            continue
        actual = actual_usage_sync(company, metric)
        if actual is None:
            continue
        stored = usage_sync(company, metric)
        if stored == actual:
            continue
        UsageCounter.objects.update_or_create(
            company=company, metric=metric, period='', defaults={'value': actual},
        )
        corrections.append({'metric': metric, 'was': stored, 'now': actual})
    return corrections
