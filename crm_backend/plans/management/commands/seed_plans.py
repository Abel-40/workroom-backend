from django.core.management.base import BaseCommand
from plans.models import Plan

class Command(BaseCommand):
    help = "Seed default subscription plans into the Plan model"

    def handle(self, *args, **options):
        plans = [
            {
                "name": "Free",
                "price": 0.00,
                "description": "Basic plan for small teams. Limited features.",
                "max_departments": 2,
                "max_users": 10,
                "max_projects": 4,
                "max_tasks": 8,
                "trial_days": 0
            },
            {
                "name": "Starter",
                "price": 9.00,
                "description": "Great for small teams. Includes a 7-day trial.",
                "max_departments": 5,
                "max_users": 25,
                "max_projects": 10,
                "max_tasks": 100,
                "trial_days": 7
            },
            {
                "name": "Team",
                "price": 29.00,
                "description": "Perfect for growing teams. Includes more space and features.",
                "max_departments": 10,
                "max_users": 50,
                "max_projects": 25,
                "max_tasks": 500,
                "trial_days": 14
            },
            {
                "name": "Business",
                "price": 59.00,
                "description": "Advanced features for large organizations.",
                "max_departments": 25,
                "max_users": 200,
                "max_projects": 100,
                "max_tasks": 2000,
                "trial_days": 14
            },
            {
                "name": "Enterprise",
                "price": 0.00,
                "description": "Custom plan for enterprise clients. Contact sales.",
                "max_departments": 0,
                "max_users": 0,
                "max_projects": 0,
                "max_tasks": 0,
                "trial_days": 30
            }
        ]

        for plan_data in plans:
            plan, created = Plan.objects.get_or_create(name=plan_data["name"], defaults=plan_data)
            if created:
                self.stdout.write(self.style.SUCCESS(f"✓ Created: {plan.name}"))
            else:
                self.stdout.write(self.style.WARNING(f"⟳ Already exists: {plan.name}"))
