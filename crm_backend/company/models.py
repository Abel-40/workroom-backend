from django.db import models
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from plans.models import Plan
from utils.models import UUIDModel

class Sector(UUIDModel):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

class Company(UUIDModel):
    SECTOR_CHOICES = [
        ('software', 'Software'),
        ('finance', 'Finance'),
        ('marketing', 'Marketing'),
        ('education', 'Education'),
        # Add more as needed
    ]
    """Main company entity for each customer using the platform"""
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=10, unique=True,null=True,blank=True)  
    created_at = models.DateTimeField(auto_now_add=True)

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='owned_company'
    )
    sector = models.ForeignKey(
        Sector,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='companies',
    )
    
    # Subscription-related fields
    plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, null=True)

    subscription_status = models.CharField(
        max_length=20,
        choices=[
            ('inactive', 'Inactive'),
            ('trialing', 'Trialing'),
            ('active', 'Active'),
            ('canceled', 'Canceled'),
        ],
        default='inactive'
    )

    is_trial = models.BooleanField(default=False)
    trial_end = models.DateTimeField(blank=True, null=True)

    
    ai_agent_enabled = models.BooleanField(default=False)

    def on_trial(self):
        return self.is_trial and self.trial_end and timezone.now() < self.trial_end

    def is_active(self):
        return self.subscription_status == 'active' or self.on_trial()

    def __str__(self):
        return self.name
    class Meta:
        unique_together = ('name','owner')