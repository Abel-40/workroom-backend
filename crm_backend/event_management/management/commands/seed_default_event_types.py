from django.core.management.base import BaseCommand

from event_management.models import DefaultEventType


class Command(BaseCommand):
    help = "Seed default event types offered during company onboarding"

    # All global (sector=None): unlike task types/departments, event
    # categories aren't observed to vary meaningfully by company sector --
    # the real differentiator (e.g. a fully remote company skipping the
    # social ones) is company preference, which the enable/don't-enable step
    # already handles.
    GLOBAL_EVENT_TYPES = [
        {"name": "Meeting", "description": "General team or cross-department meeting."},
        {"name": "Training", "description": "Skill-building, onboarding, or compliance training session."},
        {"name": "Company Announcement", "description": "Company-wide announcement or all-hands."},
        {"name": "Coffee Time", "description": "Informal virtual or in-person coffee chat."},
        {"name": "Birthday", "description": "A team member's birthday."},
        {"name": "Office Social", "description": "In-person social gathering, e.g. a happy hour or office party."},
    ]

    def handle(self, *args, **options):
        for data in self.GLOBAL_EVENT_TYPES:
            self._create(data, sector=None)

    def _create(self, data, sector):
        event_type, created = DefaultEventType.objects.get_or_create(
            name=data["name"], sector=sector, defaults={"description": data["description"]},
        )
        label = sector.name if sector else "All Sectors"
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created event type: {event_type.name} ({label})"))
        else:
            self.stdout.write(self.style.WARNING(f"Already exists: {event_type.name} ({label})"))
