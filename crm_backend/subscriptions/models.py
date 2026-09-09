from django.db import models
from django.utils import timezone
from utils.models import UUIDModel

class Subscription(UUIDModel):
    STATUS_CHOICES = [
        ('trialing', 'Trialing'),
        ('active', 'Active'),
        ('past_due', 'Past Due'),
        ('canceled', 'Canceled'),
        ('incomplete', 'Incomplete'),
        ('incomplete_expired', 'Incomplete Expired'),
    ]

    company = models.OneToOneField('company.Company', on_delete=models.CASCADE, related_name='subscription')
    plan = models.ForeignKey('plans.Plan', on_delete=models.SET_NULL, null=True)
    
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, null=True)

    # The plan's limits as they were when this subscription started, resolved
    # *before* the live Plan row (see entitlements.services.resolve).
    #
    # This is the point of the whole field: changing `team`'s numbers must
    # never silently alter what an existing customer is already paying for.
    # An FK alone cannot express that -- it follows the row wherever it goes.
    # Empty for subscriptions that predate this, which fall back to the live
    # plan; see the resolver.
    plan_snapshot = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='incomplete')
    is_trial = models.BooleanField(default=False)
    # When `status` became past_due. The grace period is measured from here
    # rather than from `updated_at`, which any unrelated write would reset --
    # and a grace period that silently restarts is not a grace period.
    past_due_since = models.DateTimeField(null=True, blank=True)

    start_date = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField(blank=True, null=True)
    canceled_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_active(self):
        return self.status == 'active' or (self.is_trial and self.current_period_end and timezone.now() < self.current_period_end)

    def on_trial(self):
        return self.is_trial and self.current_period_end and timezone.now() < self.current_period_end

    def __str__(self):
        return f"{self.company.name} - {self.status}"
