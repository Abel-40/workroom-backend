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

    ``audience`` answers "who is this for", and it answers both halves of the
    authorization question at once: who may see the event, and who was allowed
    to schedule it in the first place. Before it, every event was company-wide
    and any member could create one, which made "book a room with two people"
    and "announce the all-hands" the same action.

    The two ends of the range are the ones worth stating:

    ``personal``
        The organizer's own entry. Exactly one reader, and **no
        administrative override** -- not the company owner, not a company
        manager. This matches the ``personal`` scope on ``documents.Document``
        rather than the rest of this model, deliberately.
    ``company``
        Every active member sees it; only the Owner or a company manager may
        schedule one. This is what every event created before the field
        existed became, so no existing event changed hands or changed
        audience (see migration 0003).

    ``department`` and ``team`` follow the obvious structural match;
    ``custom`` is a meeting with named attendees and is open to anyone to
    create, because that is ordinary collaboration rather than a governance
    action. The full matrix lives in
    :mod:`event_management.services` -- ``user_can_view_event``,
    ``user_can_manage_event`` and ``can_create_event_with_audience``.
    """

    class Audience(models.TextChoices):
        PERSONAL = 'personal', 'Personal'
        CUSTOM = 'custom', 'Custom'
        TEAM = 'team', 'Team'
        DEPARTMENT = 'department', 'Department'
        COMPANY = 'company', 'Company'

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='events')
    # Defaults to the most restrictive audience, not the widest: a client that
    # has not been updated yet creates an entry only its organizer can see,
    # rather than one it silently broadcast to the company.
    audience = models.CharField(max_length=20, choices=Audience.choices, default=Audience.PERSONAL)
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
    # DEPRECATED -- superseded by EventAttendee, which carries a response.
    #
    # Kept for one release and dual-written by the service layer so a rollback
    # does not lose who was invited to what. Nothing should *read* it: both
    # the ``custom`` audience check and the attendee list go through
    # EventAttendee. Remove the field, and the dual-write in
    # event_management.services._sync_attendees, once the release carrying
    # migration 0003 has shipped.
    attendees = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='events_attending', blank=True)
    # One RFC 5545 RRULE, e.g. 'FREQ=WEEKLY;BYDAY=MO,WE'. Descriptive metadata
    # only -- nothing expands it into occurrences, and there are no generated
    # per-occurrence rows. See event_management.recurrence.
    recurrence_rule = models.CharField(max_length=500, blank=True, default='')
    # DEPRECATED -- superseded by recurrence_rule, which can express what these
    # three could plus everything they could not. Dual-written from the rule
    # for one release; see event_management.recurrence.legacy_from_rrule for
    # what survives the projection and what does not.
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
        indexes = [
            # The calendar read: this company's events, in a date window,
            # narrowed by audience.
            models.Index(fields=['company', 'audience', 'start_at']),
        ]

    def __str__(self):
        return f'{self.title} ({self.company_id})'


class EventAttendee(UUIDModel):
    """One person invited to one event, and what they said about it.

    Replaces the bare ``Event.attendees`` M2M. The M2M could record that
    somebody was invited and nothing else -- no accept, no decline, no "when
    did they answer" -- so an organizer looking at a meeting could not tell an
    empty room from a full one.

    For a ``custom`` event these rows *are* the audience: they decide who may
    see it, which is why every id is validated against the event's own company
    before a row is written (see ``_resolve_attendees``). Adding somebody to
    an event is a grant, and it is the only grant a non-manager can make.
    """

    class Response(models.TextChoices):
        NO_RESPONSE = 'no_response', 'No response'
        ACCEPTED = 'accepted', 'Accepted'
        DECLINED = 'declined', 'Declined'
        TENTATIVE = 'tentative', 'Tentative'

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='attendee_records')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='event_attendances',
    )
    response = models.CharField(max_length=20, choices=Response.choices, default=Response.NO_RESPONSE)
    # Null until they actually answer, so "never responded" and "responded
    # no_response" stay distinguishable.
    responded_at = models.DateTimeField(null=True, blank=True)
    # Who put them on the list. SET_NULL so the invitation survives the
    # inviter leaving -- same reasoning as ProjectMembership.added_by.
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['event', 'user'], name='one_attendee_row_per_event_user'),
        ]
        indexes = [
            # "which events am I on" -- the `mine` filter and the custom-audience
            # visibility clause both start here.
            models.Index(fields=['user', 'event']),
        ]

    def __str__(self):
        return f'{self.user_id} on {self.event_id} ({self.response})'
