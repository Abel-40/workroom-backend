"""Seed the four plans with the limits §11 specifies.

The numbers are taken verbatim from the table in §11 and are not to be
invented or adjusted here. `null` means unlimited throughout; for
`ai_planning_generations_per_month` specifically, `0` means disabled and is a
different thing from `null` -- Free is 0, Business is null.

Matched on `key`, falling back to `name`, and updated in place rather than
created blindly, because a company may already point at a row with one of these
names. Updating
the live `Plan` deliberately does **not** change what existing subscribers
have: their limits come from `Subscription.plan_snapshot`, which is exactly the
isolation this arrangement exists to provide.
"""

from django.db import migrations

FEATURES_TEAM = [
    'workload_policy_warn',
    'skills_matching',
    'department_analytics',
    'audit_log_view',
    'public_projects',
]
FEATURES_BUSINESS = FEATURES_TEAM + [
    'workload_policy_block',
    'company_analytics',
    'audit_log_export',
    'custom_roles',
    'integrations',
]
# The SSO key is reserved and unimplemented -- §11 asks for the key to exist
# so nothing has to migrate when it is built.
FEATURES_ENTERPRISE = FEATURES_BUSINESS + ['sso']

GIGABYTE = 1024 ** 3

PLANS = [
    {
        'key': 'free', 'name': 'Free',
        'max_members': 5, 'max_active_projects': 3,
        'max_departments_limit': 1, 'max_teams': 0,
        'storage_bytes': 2 * GIGABYTE, 'info_portal_page_limit': 50,
        'ai_credits_per_month': 50, 'ai_credit_pool_basis': 'flat',
        'ai_model_tier': 'economy', 'ai_planning_generations_per_month': 0,
        'features': [], 'max_integrations': 0,
    },
    {
        'key': 'team', 'name': 'Team',
        'max_members': 50, 'max_active_projects': None,
        'max_departments_limit': None, 'max_teams': None,
        'storage_bytes': 100 * GIGABYTE, 'info_portal_page_limit': None,
        'ai_credits_per_month': 300, 'ai_credit_pool_basis': 'per_user',
        'ai_model_tier': 'standard', 'ai_planning_generations_per_month': 5,
        'features': FEATURES_TEAM, 'max_integrations': 0,
    },
    {
        'key': 'business', 'name': 'Business',
        'max_members': None, 'max_active_projects': None,
        'max_departments_limit': None, 'max_teams': None,
        'storage_bytes': 1024 * GIGABYTE, 'info_portal_page_limit': None,
        'ai_credits_per_month': 1000, 'ai_credit_pool_basis': 'per_user',
        'ai_model_tier': 'premium', 'ai_planning_generations_per_month': None,
        'features': FEATURES_BUSINESS, 'max_integrations': None,
    },
    {
        'key': 'enterprise', 'name': 'Enterprise',
        'max_members': None, 'max_active_projects': None,
        'max_departments_limit': None, 'max_teams': None,
        # "custom" in the table: no ceiling in the product, negotiated
        # commercially. null is how this schema says that.
        'storage_bytes': None, 'info_portal_page_limit': None,
        'ai_credits_per_month': None, 'ai_credit_pool_basis': 'per_user',
        'ai_model_tier': 'premium', 'ai_planning_generations_per_month': None,
        'features': FEATURES_ENTERPRISE, 'max_integrations': None,
    },
]


def forwards(apps, schema_editor):
    Plan = apps.get_model('plans', 'Plan')
    for entry in PLANS:
        # Copy before popping: PLANS is module-level, and mutating it would
        # make a second run of this function raise KeyError on the first plan.
        spec = dict(entry)
        key = spec.pop('key')
        name = spec.pop('name')
        existing = Plan.objects.filter(key=key).first() or Plan.objects.filter(name=name).first()
        if existing is None:
            Plan.objects.create(key=key, name=name, **spec)
            continue
        existing.key = key
        existing.name = name
        for field, value in spec.items():
            setattr(existing, field, value)
        existing.save()


def backwards(apps, schema_editor):
    """Clears the key and the new limits, leaving the rows themselves alone.

    Deleting them would orphan every `Subscription.plan` pointing at one, and
    a subscription with no plan is a worse state than a plan with no limits.
    """
    Plan = apps.get_model('plans', 'Plan')
    Plan.objects.filter(key__in=('free', 'team', 'business', 'enterprise')).update(
        key=None,
        max_members=None, max_active_projects=None, max_departments_limit=None, max_teams=None,
        storage_bytes=None, info_portal_page_limit=None, ai_credits_per_month=None,
        ai_planning_generations_per_month=None, features=[], max_integrations=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('plans', '0002_plan_ai_credit_pool_basis_plan_ai_credits_per_month_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
