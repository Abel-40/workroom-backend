"""Exactly what the AI is allowed to see, and nothing else.

This module is an **allow-list**, not an exclude-list, and the difference is
the whole point. An exclude-list is a promise that somebody remembered every
field worth hiding; it fails silently the day a new column lands on
``CompanyUserProfile``, because nothing had to be changed for the new field to
start being sent. An allow-list fails in the other direction -- a new field is
absent until somebody writes its name here, on purpose, in a diff a reviewer
can see.

**Sent** (§4): project title, brief and deadline; department and task-type
names; the skill catalog's names; the human-approved candidate pool; the
requester's timezone; the hard task cap; the company's assignment-policy
limits.

**Never sent**: contact information, résumés, birthdays, addresses, phone
numbers, profile pictures, other projects, other users' AI conversations,
billing or subscription data, and real user ids. ``ai_agent/test_context.py``
asserts this against a fixture whose every forbidden field is populated with a
findable sentinel, so the test fails if any of it ever reaches the payload by
any route.

**People are opaque refs.** The model receives ``member_1``, not a UUID, and
returns ``member_1`` in its suggestions. The mapping lives on the generation
row and is regenerated per generation, so a ref carries no meaning outside the
one plan it was minted for and cannot be correlated across projects. A ref is
also useless to a caller who somehow obtains one: it is resolved only through
``resolve_assignee_ref``, against that generation's own map.
"""

import logging
from decimal import Decimal

from departments_and_teams.models import Department
from projects_and_tasks.models import TaskType
from users.models import CompanyUserProfile
from workforce.models import AssignmentPolicy, MemberSkill, Skill
from workforce.workload import effective_enforcement_sync, member_workload_sync

logger = logging.getLogger(__name__)

REF_PREFIX = 'member_'

# The candidate-pool keys §4 names. Written out so the serializer can assert
# its own output shape -- a typo here becomes a failing test rather than a
# field the model silently never sees.
CANDIDATE_FIELDS = frozenset({
    'opaque_ref', 'display_name', 'profession', 'skills',
    'department', 'team', 'capacity_hours', 'committed_hours', 'active_task_count',
})

# Top-level keys the request may carry. Anything else is a bug in this module.
CONTEXT_FIELDS = frozenset({
    'generation_id', 'project_id', 'title', 'description', 'requirements', 'brief',
    'deadline', 'departments', 'task_types', 'skill_catalog', 'candidates',
    'requester_timezone', 'max_tasks', 'assignment_limits',
})


def build_assignee_refs(user_ids) -> dict:
    """Mint this generation's ref-to-user map.

    Sequential and per generation, deliberately: a random token would be no
    more private (the map is what makes a ref meaningful, not its shape) and
    would be harder to read in a log line when something goes wrong.
    """
    return {f'{REF_PREFIX}{index}': str(user_id) for index, user_id in enumerate(sorted(map(str, user_ids)), start=1)}


def resolve_assignee_ref(generation, ref):
    """Turn a ref the model returned back into a user id, or None.

    The only route from a ref to a person. An unknown ref -- invented,
    hallucinated, or copied from another generation -- resolves to None and is
    handled by the caller as an out-of-pool suggestion.
    """
    if not ref or not isinstance(ref, str):
        return None
    return (generation.assignee_refs or {}).get(ref)


def _display_name(user) -> str:
    """A human label, not an identifier. Falls back through the same chain
    the rest of the product uses so the model sees what a person would."""
    return user.get_full_name() or user.first_name or user.username


def _skills_for(profile) -> list:
    return [
        {'name': row.skill.name, 'level': row.level}
        for row in MemberSkill.objects.filter(profile=profile).select_related('skill').order_by('skill__name')
    ]


def build_candidate_pool(company, user_ids, refs) -> list:
    """The people the model may suggest, and the little it needs to choose.

    Every entry comes from a ``CompanyUserProfile`` in *this* company. A user
    id in ``user_ids`` without a membership here is skipped rather than
    partially described -- somebody who is not a member of the company is not
    a candidate, whatever the caller sent.
    """
    by_user_id = {str(user_id): ref for ref, user_id in refs.items()}
    profiles = CompanyUserProfile.objects.filter(
        company=company, user_id__in=list(user_ids), is_active=True,
    ).select_related('user', 'department', 'profession_ref')

    pool = []
    for profile in profiles:
        ref = by_user_id.get(str(profile.user_id))
        if ref is None:
            continue
        workload = member_workload_sync(company, profile.user, profile=profile)
        team = profile.user.team_memberships.filter(company=company).values_list('name', flat=True).first()
        pool.append({
            'opaque_ref': ref,
            'display_name': _display_name(profile.user),
            'profession': profile.profession_ref.name if profile.profession_ref_id else None,
            'skills': _skills_for(profile),
            'department': profile.department.name if profile.department_id else None,
            'team': team,
            'capacity_hours': float(workload.capacity_hours) if workload.capacity_hours is not None else None,
            'committed_hours': float(workload.committed_hours),
            'active_task_count': workload.active_task_count,
        })
    return pool


def _assignment_limits(company) -> dict:
    """What the model should treat as "already full".

    Sent so a plan does not arrive stacked entirely on one person and then get
    flagged task by task. It is guidance, not enforcement: Django re-checks
    every suggestion on the way back, and the limits are re-read then.
    """
    policy = AssignmentPolicy.objects.filter(company=company).first()
    enforcement = effective_enforcement_sync(company, policy)
    if enforcement == 'off' or policy is None:
        return {'enforcement': 'off', 'max_active_tasks': None, 'max_utilisation_pct': None}
    return {
        'enforcement': enforcement,
        'max_active_tasks': policy.max_active_tasks,
        'max_utilisation_pct': policy.max_utilisation_pct,
    }


def _project_brief(project) -> dict | None:
    """The structured brief, if the project has one.

    Reads through ``getattr`` rather than ``project.brief`` directly: a
    project's brief row is created on first read of the brief endpoint (see
    ``projects_and_tasks.services.get_or_create_brief``), not at project
    creation, so most projects genuinely have none yet.
    ``Project.brief.RelatedObjectDoesNotExist`` is deliberately also an
    ``AttributeError`` in Django's reverse-OneToOne descriptor, which is what
    makes the ``getattr`` default work here instead of raising. The key is
    simply absent from the payload for such a project -- the honest
    representation of "there is no brief" -- rather than sent as a null.
    """
    brief = getattr(project, 'brief', None)
    if brief is None:
        return None
    return {
        field: getattr(brief, field, '') or ''
        for field in ('objective', 'background', 'scope_in', 'scope_out', 'expected_outcome', 'constraints')
    }


def build_generation_context(generation, *, requester_timezone='UTC') -> dict:
    """The complete request payload for a plan generation.

    Assembled field by field. Nothing in here spreads a model instance into a
    dict, and nothing takes ``__dict__``, ``values()`` without an explicit
    field list, or a serializer with ``exclude``. That is not fussiness: every
    one of those is a way for a column added next year to reach a third party
    without anybody deciding it should.
    """
    project = generation.project
    company = project.company
    refs = generation.assignee_refs or {}

    context = {
        'generation_id': str(generation.id),
        'project_id': str(project.id),
        'title': project.title,
        'description': project.description or '',
        'requirements': generation.prompt or '',
        'deadline': project.deadline.isoformat() if project.deadline else None,
        # Names only. The model has no use for an id it cannot act on, and
        # every id sent is one that can come back and has to be re-validated.
        'departments': sorted(
            Department.objects.filter(company=company).values_list('name', flat=True),
        ),
        'task_types': sorted(
            TaskType.objects.filter(company=company).values_list('name', flat=True),
        ),
        'skill_catalog': sorted(
            Skill.objects.filter(company=company).values_list('name', flat=True),
        ),
        'candidates': build_candidate_pool(company, generation.requested_assignee_ids or [], refs),
        'requester_timezone': requester_timezone or 'UTC',
        'max_tasks': generation.max_tasks,
        'assignment_limits': _assignment_limits(company),
    }

    brief = _project_brief(project)
    if brief is not None:
        context['brief'] = brief

    _assert_shape(context)
    return context


def _assert_shape(context: dict):
    """Fail loudly if the payload ever grows a key nobody allowed.

    Cheap, and it runs in production rather than only under test. The failure
    mode this guards against is somebody adding a key here and not to
    ``CONTEXT_FIELDS`` -- which is exactly the moment a second pair of eyes is
    worth something, and exactly the moment nobody is looking.
    """
    unexpected = set(context) - CONTEXT_FIELDS
    if unexpected:
        raise ValueError(f'AI context carries fields nobody allow-listed: {sorted(unexpected)}')
    for candidate in context.get('candidates', []):
        extra = set(candidate) - CANDIDATE_FIELDS
        if extra:
            raise ValueError(f'AI candidate entry carries fields nobody allow-listed: {sorted(extra)}')


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """Cost in USD, or None when this provider/model pair has no published
    rate here.

    None rather than zero on purpose: "we do not know what this cost" and "it
    cost nothing" are different facts, and a zero would quietly under-report a
    bill somebody eventually has to pay.
    """
    rate = PRICE_PER_MILLION_TOKENS.get((provider or '').lower(), {}).get((model or '').lower())
    if rate is None:
        return None
    prompt_rate, completion_rate = rate
    return (
        Decimal(input_tokens or 0) * Decimal(str(prompt_rate))
        + Decimal(output_tokens or 0) * Decimal(str(completion_rate))
    ) / Decimal(1_000_000)


# USD per million tokens, (input, output). A small table on purpose: an
# unlisted model records tokens and a null cost rather than a guessed one, and
# the tokens are the part that cannot be recomputed later.
PRICE_PER_MILLION_TOKENS = {
    'openai': {
        'gpt-4o': (2.50, 10.00),
        'gpt-4o-mini': (0.15, 0.60),
    },
    'anthropic': {
        'claude-opus-5': (5.00, 25.00),
        'claude-sonnet-5': (3.00, 15.00),
        'claude-haiku-4-5-20251001': (1.00, 5.00),
    },
    'gemini': {
        'gemini-2.0-flash': (0.10, 0.40),
    },
}
