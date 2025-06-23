from django.db import models

# Create your models here.
class Plan(models.Model):
    """Predefined subscription plans created by the SaaS owner"""
    name = models.CharField(max_length=100, unique=True)
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    description = models.TextField(blank=True)
    stripe_price_id = models.CharField(max_length=255, blank=True, null=True)

    max_departments = models.PositiveIntegerField(default=0)
    max_users = models.PositiveIntegerField(default=0)
    max_projects = models.PositiveIntegerField(default=0)
    max_tasks = models.PositiveIntegerField(default=0)

    trial_days = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.name