from django.db import models
from utils.models import UUIDModel


class Plan(UUIDModel):
    """A subscription tier and the limits that come with it.

    Extended rather than replaced: `name`, `price`, `stripe_price_id` and the
    original four `max_*` fields predate this and are still what the Stripe
    checkout flow reads. The fields below are what
    :mod:`entitlements.services` resolves against.

    The older `max_users`/`max_projects`/`max_departments`/`max_tasks` are
    **deprecated** and no longer consulted by any check. They use 0 as their
    default, which is ambiguous -- 0 reads as both "none allowed" and "not
    configured" -- and that ambiguity is exactly what the nullable fields below
    fix: `null` means unlimited, and it means it unmistakably. Left in place
    rather than dropped, per the no-destructive-migrations rule.
    """

    class Key(models.TextChoices):
        FREE = 'free', 'Free'
        TEAM = 'team', 'Team'
        BUSINESS = 'business', 'Business'
        ENTERPRISE = 'enterprise', 'Enterprise'

    class CreditBasis(models.TextChoices):
        FLAT = 'flat', 'Flat per company'
        PER_USER = 'per_user', 'Per user'

    class ModelTier(models.TextChoices):
        ECONOMY = 'economy', 'Economy'
        STANDARD = 'standard', 'Standard'
        PREMIUM = 'premium', 'Premium'

    name = models.CharField(max_length=100, unique=True)
    # Nullable so existing rows (which predate this field) migrate cleanly;
    # the four seeded plans all carry one.
    key = models.CharField(max_length=20, choices=Key.choices, null=True, blank=True, unique=True)
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    description = models.TextField(blank=True)
    stripe_price_id = models.CharField(max_length=255, blank=True, null=True)

    # DEPRECATED -- see the class docstring. Nothing reads these.
    max_departments = models.PositiveIntegerField(default=0)
    max_users = models.PositiveIntegerField(default=0)
    max_projects = models.PositiveIntegerField(default=0)
    max_tasks = models.PositiveIntegerField(default=0)

    # Every limit below uses NULL for "unlimited". That is the whole reason
    # they are nullable rather than defaulting to a large number: a sentinel
    # like 999999 is a limit somebody eventually hits, and then nobody can
    # tell whether it was meant.
    max_members = models.IntegerField(null=True, blank=True)
    max_active_projects = models.IntegerField(null=True, blank=True)
    max_departments_limit = models.IntegerField(null=True, blank=True)
    max_teams = models.IntegerField(null=True, blank=True)
    storage_bytes = models.BigIntegerField(null=True, blank=True)
    info_portal_page_limit = models.IntegerField(null=True, blank=True)

    ai_credits_per_month = models.IntegerField(null=True, blank=True)
    ai_credit_pool_basis = models.CharField(
        max_length=20, choices=CreditBasis.choices, default=CreditBasis.FLAT,
    )
    ai_model_tier = models.CharField(
        max_length=20, choices=ModelTier.choices, default=ModelTier.ECONOMY,
    )
    # null = unlimited, 0 = disabled. Both are meaningful here and they are
    # not the same thing: Free is 0, Business is null.
    ai_planning_generations_per_month = models.IntegerField(null=True, blank=True)

    features = models.JSONField(default=list, blank=True)
    max_integrations = models.IntegerField(null=True, blank=True)

    trial_days = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.name

    def as_snapshot(self) -> dict:
        """The limits, frozen, for `Subscription.plan_snapshot`.

        Only limits and features -- deliberately not price or Stripe ids,
        which are commercial facts that belong on the live row and would go
        stale here without anyone noticing.
        """
        return {
            'key': self.key,
            'name': self.name,
            'max_members': self.max_members,
            'max_active_projects': self.max_active_projects,
            'max_departments': self.max_departments_limit,
            'max_teams': self.max_teams,
            'storage_bytes': self.storage_bytes,
            'info_portal_pages': self.info_portal_page_limit,
            'ai_credits': self.ai_credits_per_month,
            'ai_credit_pool_basis': self.ai_credit_pool_basis,
            'ai_model_tier': self.ai_model_tier,
            'ai_planning_generations': self.ai_planning_generations_per_month,
            'integrations': self.max_integrations,
            'features': list(self.features or []),
        }
