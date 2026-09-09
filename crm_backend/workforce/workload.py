"""How much somebody is carrying, and whether one more is too much.

Two numbers, for two different jobs, and they are deliberately not merged:

``active_task_count``
    The cheap guardrail. Counting open tasks needs no estimates and no stated
    capacity, so it works for every company on day one. This is what a hard
    limit should be built on.

``committed_hours`` against prorated ``capacity_hours``
    The meaningful figure, and much more conditional. It is the one a manager
    should actually look at, and it is only true when the tasks involved carry
    estimates and the person has stated a weekly capacity.

Where the hours figure is incomplete this module says so, rather than
reporting a smaller number. A task with no estimate contributes zero hours,
which makes an overloaded person look free -- so ``hours_complete`` travels
with the number everywhere it goes, and the utilisation limit does not fire
when it is False. A limit that silently under-counts is worse than no limit:
it produces confident, wrong answers, and people stop believing the ones that
are right.

Nothing here assigns anything. There is no load balancer, no auto-assignment
and no scoring engine, deliberately -- this answers "is this particular
assignment over the line" and hands the decision back to a person.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from entitlements.services import has_feature_sync
from projects_and_tasks.models import Project, Task
from users.models import CompanyUserProfile

from .models import AssignmentPolicy
from .services import DEFAULT_WORKING_DAYS

logger = logging.getLogger(__name__)

# Two weeks. Long enough that a single busy day does not read as an overload,
# short enough that a deadline three months out does not count against
# somebody's capacity today.
HORIZON_DAYS = 14

WEEKDAY_TOKENS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')

# The statuses that mean "still owed". DONE is excluded; everything else on a
# live project counts, including IN_REVIEW -- work awaiting a decision is
# still work the assignee may get back.
OPEN_STATUSES = (Task.STATUS.TODO, Task.STATUS.IN_PROGRESS, Task.STATUS.IN_REVIEW)

FEATURE_WARN = 'workload_policy_warn'
FEATURE_BLOCK = 'workload_policy_block'


@dataclass(frozen=True)
class MemberWorkload:
    """A snapshot. Never stored -- recomputed at the moment it is asked for,
    because a cached workload is wrong the instant anybody is assigned
    anything."""

    active_task_count: int
    committed_hours: Decimal
    tasks_missing_estimate: int
    capacity_hours: Decimal | None
    horizon_days: int

    @property
    def hours_complete(self) -> bool:
        """Whether ``committed_hours`` is the whole story."""
        return self.tasks_missing_estimate == 0

    @property
    def utilisation_pct(self) -> Decimal | None:
        """Committed against capacity, or None when the question cannot be
        answered honestly -- no stated capacity, or a capacity of zero."""
        if self.capacity_hours is None or self.capacity_hours <= 0:
            return None
        raw = (self.committed_hours / self.capacity_hours) * 100
        return raw.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)

    def as_dict(self) -> dict:
        utilisation = self.utilisation_pct
        return {
            'active_task_count': self.active_task_count,
            'committed_hours': float(self.committed_hours),
            'capacity_hours': float(self.capacity_hours) if self.capacity_hours is not None else None,
            'utilisation_pct': float(utilisation) if utilisation is not None else None,
            'hours_complete': self.hours_complete,
            'tasks_missing_estimate': self.tasks_missing_estimate,
            'horizon_days': self.horizon_days,
        }


def prorated_capacity_hours(profile, *, horizon_days=HORIZON_DAYS, today=None) -> Decimal | None:
    """Capacity over the horizon, adjusted for the working week and time off.

    Returns None when no weekly capacity is stated. Unstated is not zero: a
    member who has never opened the setting should read as "unknown", which
    stops a limit firing on a number nobody supplied.
    """
    if profile is None or profile.weekly_capacity_hours is None:
        return None

    availability = profile.availability if isinstance(profile.availability, dict) else {}
    working_days = [d for d in (availability.get('working_days') or DEFAULT_WORKING_DAYS) if d in WEEKDAY_TOKENS]
    if not working_days:
        return Decimal('0')

    today = today or timezone.localdate()
    off = _time_off_dates(availability.get('time_off') or [])
    working_day_count = sum(
        1 for offset in range(horizon_days)
        if (day := today + timedelta(days=offset)) not in off
        and WEEKDAY_TOKENS[day.weekday()] in working_days
    )
    per_day = Decimal(profile.weekly_capacity_hours) / Decimal(len(working_days))
    return (per_day * working_day_count).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _time_off_dates(periods) -> set:
    """Expand validated ``{'start', 'end'}`` ranges into a set of dates.

    Ranges are inclusive at both ends -- "off from the 1st to the 5th" means
    five days, not four. Bounded by a sanity cap so a malformed range that got
    past validation cannot expand into an unbounded loop.
    """
    dates = set()
    for period in periods:
        try:
            start = date.fromisoformat(str(period.get('start')))
            end = date.fromisoformat(str(period.get('end')))
        except (ValueError, TypeError, AttributeError):
            continue
        if end < start or (end - start).days > 366:
            continue
        dates.update(start + timedelta(days=offset) for offset in range((end - start).days + 1))
    return dates


def member_workload_sync(company, user, *, horizon_days=HORIZON_DAYS, profile=None,
                          today=None) -> MemberWorkload:
    """What ``user`` is currently carrying in ``company``.

    Both figures come from one aggregate query rather than one per member, so
    a roster of fifty does not cost fifty round trips.

    The two figures count different things on purpose. ``active_task_count``
    is every open task -- a task due in March is still a thing you are on the
    hook for. ``committed_hours`` counts only what falls inside the horizon,
    including anything already overdue, because that is what the next
    fortnight actually looks like.
    """
    if profile is None:
        profile = CompanyUserProfile.objects.filter(company=company, user=user).first()

    today = today or timezone.localdate()
    horizon_end = timezone.now() + timedelta(days=horizon_days)

    open_tasks = Task.objects.filter(
        assigned_to=user, is_deleted=False, status__in=OPEN_STATUSES,
        project__company=company, project__is_deleted=False, project__status=Project.STATUS.ACTIVE,
    )
    in_horizon = Q(deadline__lte=horizon_end)
    aggregate = open_tasks.aggregate(
        active=Count('id'),
        committed=Sum('estimated_time', filter=in_horizon),
        missing=Count('id', filter=in_horizon & Q(estimated_time__isnull=True)),
    )

    committed = aggregate['committed'] or timedelta()
    return MemberWorkload(
        active_task_count=aggregate['active'] or 0,
        committed_hours=(
            Decimal(committed.total_seconds()) / Decimal(3600)
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
        tasks_missing_estimate=aggregate['missing'] or 0,
        capacity_hours=prorated_capacity_hours(profile, horizon_days=horizon_days, today=today),
        horizon_days=horizon_days,
    )


member_workload = sync_to_async(member_workload_sync, thread_sensitive=True)


# --------------------------------------------------------------------------
# The policy
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AssignmentDecision:
    """The answer to "may this assignment go ahead", and why.

    ``allowed=True`` with a non-empty ``breached`` is the warn case, and it is
    the common one: the assignment proceeds and the caller is handed the
    number to show. That is the whole point of warn mode -- somebody sees a
    figure they did not have, in one click, rather than being stopped.
    """

    allowed: bool
    enforcement: str            # 'off' | 'warn' | 'block'
    breached: list = field(default_factory=list)
    workload: MemberWorkload | None = None
    limits: dict = field(default_factory=dict)
    needs_approval: bool = False

    @property
    def warned(self) -> bool:
        return self.allowed and bool(self.breached)

    def as_dict(self) -> dict:
        return {
            'enforcement': self.enforcement,
            'breached': list(self.breached),
            'limits': dict(self.limits),
            'needs_approval': self.needs_approval,
            'workload': self.workload.as_dict() if self.workload else None,
        }


def effective_enforcement_sync(company, policy) -> str:
    """What the policy is actually allowed to do on this plan.

    Free has no policy at all -- not even warn -- so a company without
    ``workload_policy_warn`` gets ``off`` regardless of what the row says.
    A company with warn but not block *degrades to warn* rather than to off:
    they configured a limit and they are entitled to be told about it, they
    are simply not entitled to have it stop anybody.
    """
    if policy is None or not policy.enabled:
        return 'off'
    if not has_feature_sync(company, FEATURE_WARN):
        return 'off'
    if policy.enforcement == AssignmentPolicy.Enforcement.BLOCK:
        return 'block' if has_feature_sync(company, FEATURE_BLOCK) else 'warn'
    return 'warn'


def evaluate_assignment_sync(company, user, *, profile=None, policy=None,
                              actor_role=None, override_reason=None) -> AssignmentDecision:
    """Whether giving ``user`` one more task crosses a configured limit.

    The counts are read *before* the new task is added, and each limit is a
    ceiling on the state after it -- so ``max_active_tasks=5`` means five, and
    the sixth assignment is the one refused.
    """
    if policy is None:
        policy = AssignmentPolicy.objects.filter(company=company).first()
    enforcement = effective_enforcement_sync(company, policy)
    if enforcement == 'off':
        return AssignmentDecision(allowed=True, enforcement='off')

    workload = member_workload_sync(company, user, profile=profile)
    limits = {
        'max_active_tasks': policy.max_active_tasks,
        'max_utilisation_pct': policy.max_utilisation_pct,
    }
    breached = []

    if policy.max_active_tasks is not None and workload.active_task_count + 1 > policy.max_active_tasks:
        breached.append('max_active_tasks')

    utilisation = workload.utilisation_pct
    if policy.max_utilisation_pct is not None and utilisation is not None and workload.hours_complete:
        if utilisation > policy.max_utilisation_pct:
            breached.append('max_utilisation_pct')
    elif policy.max_utilisation_pct is not None and not workload.hours_complete:
        # Deliberately not a breach and deliberately not silent: the figure is
        # short by however many tasks carry no estimate, so refusing on it
        # would be refusing on a number we know is wrong.
        logger.info(
            'workload.utilisation_incomplete',
            extra={'company_id': str(company.pk), 'user_id': str(user.pk),
                   'missing_estimates': workload.tasks_missing_estimate},
        )

    if not breached:
        return AssignmentDecision(allowed=True, enforcement=enforcement, workload=workload, limits=limits)

    if enforcement == 'warn':
        return AssignmentDecision(
            allowed=True, enforcement='warn', breached=breached, workload=workload, limits=limits,
        )

    authorized = bool(override_reason) and actor_role in (policy.override_roles or [])
    return AssignmentDecision(
        allowed=authorized, enforcement='block', breached=breached, workload=workload, limits=limits,
        # Somebody who cannot override does not get an argument they cannot
        # win -- they get a request routed to somebody who can decide.
        needs_approval=not authorized,
    )


evaluate_assignment = sync_to_async(evaluate_assignment_sync, thread_sensitive=True)


def guarded_write_sync(company, assignee, apply, *, actor_role=None, override_reason=None):
    """Check the policy and perform ``apply()`` as one atomic step.

    Returns ``(decision, result)``, with ``result`` None when the policy
    refused and ``apply`` was therefore never called.

    ``apply`` is a callable rather than the write itself because the two
    assignment paths write different things -- one updates a task, the other
    creates one -- while the check around them has to be identical. The
    alternative, checking in the caller and writing afterwards, is a
    check-then-act race: two managers both read "4 of 5" and both proceed.

    In ``block`` mode the target's membership row is locked for the duration,
    which is what serialises those two managers. The lock is taken **only** in
    block mode: warn mode enforces nothing, and serialising every assignment
    in the company to produce a message nobody is bound by would be a real
    cost for no benefit.
    """
    policy = AssignmentPolicy.objects.filter(company=company).first()
    enforcement = effective_enforcement_sync(company, policy)

    with transaction.atomic():
        profile = None
        if assignee is not None and enforcement == 'block':
            profile = CompanyUserProfile.objects.select_for_update().filter(
                company=company, user=assignee,
            ).first()

        decision = (
            AssignmentDecision(allowed=True, enforcement=enforcement) if assignee is None
            else evaluate_assignment_sync(
                company, assignee, profile=profile, policy=policy,
                actor_role=actor_role, override_reason=override_reason,
            )
        )
        if not decision.allowed:
            return decision, None
        return decision, apply()


guarded_write = sync_to_async(guarded_write_sync, thread_sensitive=True)
