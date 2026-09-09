"""Carry every existing event across to the audience model unchanged.

Three backfills, and the first one is the important one:

``audience`` -> ``company``
    Every event created before this field existed was visible to every member
    of its company, because that was the only thing an event could be. The new
    default is ``personal``, which is the right default for a *new* row and
    the wrong answer for an old one -- applied to existing data it would empty
    every company's calendar overnight. So existing rows are set to
    ``company``: current visibility preserved exactly, nobody gains sight of
    anything, nobody loses it.

``attendees`` -> ``EventAttendee``
    One row per existing invitation, ``response='no_response'`` because that
    is precisely what the old shape recorded. ``invited_by`` is left NULL: the
    M2M never stored who added whom, and inventing the organizer would be a
    guess written into an audit-adjacent field.

legacy recurrence -> ``recurrence_rule``
    The three old fields translated into the RRULE that means the same thing.
    An ``is_recurring`` flag with no cadence produces no rule, because it was
    never one.

Self-contained by design: the translation is duplicated here rather than
imported from ``event_management.recurrence``, so a later change to that
module cannot retroactively change what this migration did. Idempotent -- safe
to re-run against a database where some rows already exist.
"""

from django.db import migrations

CADENCE_TO_FREQ = {'daily': 'DAILY', 'weekly': 'WEEKLY', 'monthly': 'MONTHLY'}
DAY_TO_BYDAY = {
    'mon': 'MO', 'tue': 'TU', 'wed': 'WE', 'thu': 'TH',
    'fri': 'FR', 'sat': 'SA', 'sun': 'SU',
}
BYDAY_ORDER = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')


def _rule_for(is_recurring, cadence, days):
    if not is_recurring:
        return ''
    freq = CADENCE_TO_FREQ.get((cadence or '').lower())
    if freq is None:
        return ''
    parts = [f'FREQ={freq}']
    if freq == 'WEEKLY':
        selected = {
            DAY_TO_BYDAY[token]
            for day in days or []
            if (token := str(day).strip()[:3].lower()) in DAY_TO_BYDAY
        }
        ordered = [code for code in BYDAY_ORDER if code in selected]
        if ordered:
            parts.append(f'BYDAY={",".join(ordered)}')
    return ';'.join(parts)


def backfill(apps, schema_editor):
    Event = apps.get_model('event_management', 'Event')
    EventAttendee = apps.get_model('event_management', 'EventAttendee')

    Event.objects.update(audience='company')

    existing = set(EventAttendee.objects.values_list('event_id', 'user_id'))
    through = Event.attendees.through
    EventAttendee.objects.bulk_create([
        EventAttendee(event_id=event_id, user_id=user_id, response='no_response')
        for event_id, user_id in through.objects.values_list('event_id', 'user_id')
        if (event_id, user_id) not in existing
    ], batch_size=1000)

    to_update = []
    for event in Event.objects.filter(is_recurring=True, recurrence_rule=''):
        rule = _rule_for(event.is_recurring, event.recurrence_cadence, event.recurrence_days)
        if rule:
            event.recurrence_rule = rule
            to_update.append(event)
    if to_update:
        Event.objects.bulk_update(to_update, ['recurrence_rule'], batch_size=1000)


def unbackfill(apps, schema_editor):
    """Undo only what this migration could have written.

    An attendee row with an ``invited_by`` was created through the API after
    this shipped and is not this migration's to remove. ``audience`` is left
    alone: the field is dropped by 0002's own reverse, and a partial rollback
    that kept it should keep the value that matches what the previous release
    showed.
    """
    Event = apps.get_model('event_management', 'Event')
    EventAttendee = apps.get_model('event_management', 'EventAttendee')
    EventAttendee.objects.filter(invited_by__isnull=True, response='no_response').delete()
    Event.objects.exclude(recurrence_rule='').update(recurrence_rule='')


class Migration(migrations.Migration):

    dependencies = [
        ('event_management', '0002_event_audience_attendee_rrule'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
