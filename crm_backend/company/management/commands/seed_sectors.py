from django.core.management.base import BaseCommand

from company.models import Sector


class Command(BaseCommand):
    help = "Seed default sectors offered during company registration onboarding"

    SECTORS = [
        {"name": "Software & Technology", "description": "Companies building software products and technology services."},
        {"name": "Finance & Banking", "description": "Financial services, banking, and fintech companies."},
        {"name": "Marketing & Advertising", "description": "Marketing agencies and advertising companies."},
        {"name": "Education", "description": "Schools, universities, and edtech companies."},
        {"name": "Healthcare", "description": "Healthcare providers and health-tech companies."},
        {"name": "Retail & E-commerce", "description": "Retail businesses and online commerce companies."},
    ]

    def handle(self, *args, **options):
        for data in self.SECTORS:
            sector, created = Sector.objects.get_or_create(name=data["name"], defaults=data)
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created sector: {sector.name}"))
            else:
                self.stdout.write(self.style.WARNING(f"Already exists: {sector.name}"))
