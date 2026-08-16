from company.models import Sector
from django.core.management.base import BaseCommand

from projects_and_tasks.models import DefaultTaskType


class Command(BaseCommand):
    help = "Seed default task types offered during company onboarding"

    # sector=None means "All Sectors" -- offered regardless of the company's sector.
    GLOBAL_TASK_TYPES = [
        {"name": "Development", "description": "Building or implementing a feature."},
        {"name": "Design", "description": "UI/UX or visual design work."},
        {"name": "Bug Fix", "description": "Fixing a defect or issue."},
        {"name": "Documentation", "description": "Writing or updating documentation."},
        {"name": "Research", "description": "Investigation or discovery work."},
        {"name": "Testing", "description": "Quality assurance and testing."},
        {"name": "Meeting", "description": "Meetings, syncs, and reviews."},
        {"name": "Planning", "description": "Planning and scoping work."},
    ]

    SECTOR_TASK_TYPES = {
        "Marketing & Advertising": [
            {"name": "Campaign Planning", "description": "Planning a marketing campaign."},
            {"name": "Content Creation", "description": "Producing marketing content."},
        ],
        "Healthcare": [
            {"name": "Patient Care", "description": "Direct patient care activities."},
        ],
        "Retail & E-commerce": [
            {"name": "Inventory Management", "description": "Managing stock and inventory."},
        ],
        "Education": [
            {"name": "Lesson Planning", "description": "Preparing course or lesson materials."},
        ],
    }

    def handle(self, *args, **options):
        for data in self.GLOBAL_TASK_TYPES:
            self._create(data, sector=None)

        for sector_name, task_types in self.SECTOR_TASK_TYPES.items():
            sector = Sector.objects.filter(name=sector_name).first()
            if sector is None:
                self.stdout.write(self.style.WARNING(
                    f"Skipping '{sector_name}' task types: sector not found. Run seed_sectors first.",
                ))
                continue
            for data in task_types:
                self._create(data, sector=sector)

    def _create(self, data, sector):
        task_type, created = DefaultTaskType.objects.get_or_create(
            name=data["name"], sector=sector, defaults={"description": data["description"]},
        )
        label = sector.name if sector else "All Sectors"
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created task type: {task_type.name} ({label})"))
        else:
            self.stdout.write(self.style.WARNING(f"Already exists: {task_type.name} ({label})"))
