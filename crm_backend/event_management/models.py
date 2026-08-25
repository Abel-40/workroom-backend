from django.conf import settings
from django.db import models
from utils.models import UUIDModel


class DefaultEventType(UUIDModel):
    """Global template row, mirrors projects_and_tasks.DefaultTaskType
    exactly. sector=None means "offered to every company regardless of
    sector" -- unlike task types/departments, event categories aren't
    observed to vary meaningfully by industry, so in practice every seeded
    row uses sector=None (see seed_default_event_types); the FK stays here
    for schema parity and future use."""

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    sector = models.ForeignKey(
        'company.Sector', on_delete=models.CASCADE, related_name='default_event_types',
        null=True, blank=True,
    )

    def __str__(self):
        return f"{self.name} ({self.sector.name if self.sector else 'All Sectors'})"


class EventType(UUIDModel):
    """Company-scoped event type -- mirrors projects_and_tasks.TaskType
    exactly. Created either by copying a DefaultEventType (default_event_type
    set, see event_management.services.apply_default_event_types) or as a
    wholly custom company type (default_event_type null, see
    event_management.services.create_custom_event_type)."""

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='event_types')
    default_event_type = models.ForeignKey(
        DefaultEventType, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='company_event_types',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'name')

    def __str__(self):
        return f"{self.name} ({self.company.name})"


class Event(UUIDModel):
    """A company event -- e.g. a meeting, training, or social gathering.
    No visibility field: unlike Project, every real event type in this
    product is inherently company-wide (matches
    notifications_and_activity.CompanyActivity's "company-wide, any member
    may see it" precedent); see event_management.services.user_can_view_event
    /user_can_manage_event for the resulting authorization rule."""

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='events')
    event_type = models.ForeignKey(
        EventType, on_delete=models.SET_NULL, null=True, blank=True, related_name='events',
    )
    department = models.ForeignKey(
        'departments_and_teams.Department', on_delete=models.SET_NULL, null=True, blank=True, related_name='events',
    )
    team = models.ForeignKey(
        'departments_and_teams.Team', on_delete=models.SET_NULL, null=True, blank=True, related_name='events',
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField(null=True, blank=True)
    # Free text -- doubles as a physical address or a meeting link/dial-in,
    # same "one field, two use cases" call this codebase already makes for
    # Project.image_url (external link) vs. an uploaded image.
    location = models.CharField(max_length=500, blank=True, default='')
    # Immutable creator/organizer -- no reassignable "current owner" concept
    # like Project has, since events have no ownership-transfer workflow.
    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='organized_events',
    )
    attendees = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='events_attending', blank=True)
    # Descriptive metadata only -- no recurrence-expansion engine, no
    # generated per-occurrence rows. See event_management app design notes.
    is_recurring = models.BooleanField(default=False)
    recurrence_cadence = models.CharField(
        max_length=10, blank=True, default='',
        choices=[('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly')],
    )
    recurrence_days = models.JSONField(default=list, blank=True)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['start_at']

    def __str__(self):
        return f'{self.title} ({self.company_id})'
