"""Recurrence as one RRULE string, and the compatibility shim over it.

Nothing here expands a rule into occurrences -- that is explicitly out of
scope. What is tested is that the string stored is a real RRULE, that the
deprecated cadence fields still work for one release, and that they are
derived from the rule rather than kept in parallel.
"""

import json
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from django.test import SimpleTestCase
from django.utils import timezone

from event_management.models import Event
from event_management.recurrence import (
    InvalidRecurrenceRule,
    legacy_from_rrule,
    rrule_from_legacy,
    validate_rrule,
)

FUTURE_START_AT = (timezone.now() + timedelta(days=30)).isoformat()


class RecurrenceRuleValidationTests(SimpleTestCase):
    def test_accepts_and_normalizes_a_valid_rule(self):
        self.assertEqual(validate_rrule('RRULE:freq=weekly;byday=mo,we'), 'FREQ=WEEKLY;BYDAY=MO,WE')
        self.assertEqual(validate_rrule('  FREQ=DAILY  '), 'FREQ=DAILY')
        self.assertEqual(validate_rrule('FREQ=MONTHLY;INTERVAL=2;COUNT=6'), 'FREQ=MONTHLY;INTERVAL=2;COUNT=6')

    def test_empty_is_not_a_recurrence(self):
        self.assertEqual(validate_rrule(''), '')
        self.assertEqual(validate_rrule('   '), '')

    def test_rejects_a_rule_that_is_not_one(self):
        for bad in ('every week', 'FREQ=FORTNIGHTLY', 'BYDAY=MO', 'FREQ=WEEKLY;BYDAY=XX'):
            with self.assertRaises(InvalidRecurrenceRule, msg=bad):
                validate_rrule(bad)

    def test_rejects_a_calendar_fragment(self):
        """A rule, not a VEVENT. Anything carrying its own start date would
        make the stored value disagree with the event's start_at."""
        for bad in (
            'DTSTART:20260101T090000Z\nFREQ=DAILY',
            'FREQ=DAILY;EXDATE=20260101',
            'FREQ=DAILY\nFREQ=WEEKLY',
        ):
            with self.assertRaises(InvalidRecurrenceRule, msg=bad):
                validate_rrule(bad)

    def test_rejects_an_overlong_rule(self):
        with self.assertRaises(InvalidRecurrenceRule):
            validate_rrule('FREQ=DAILY;' + 'COUNT=1;' * 200)


class LegacyRecurrenceTranslationTests(SimpleTestCase):
    def test_legacy_fields_become_the_rule_that_means_the_same(self):
        self.assertEqual(
            rrule_from_legacy(is_recurring=True, cadence='weekly', days=['Wed', 'Mon']),
            'FREQ=WEEKLY;BYDAY=MO,WE',
        )
        self.assertEqual(rrule_from_legacy(is_recurring=True, cadence='daily', days=[]), 'FREQ=DAILY')

    def test_a_flag_with_no_cadence_was_never_a_rule(self):
        self.assertEqual(rrule_from_legacy(is_recurring=True, cadence='', days=[]), '')
        self.assertEqual(rrule_from_legacy(is_recurring=False, cadence='weekly', days=['Mon']), '')

    def test_days_are_ignored_for_a_cadence_that_has_none(self):
        self.assertEqual(rrule_from_legacy(is_recurring=True, cadence='monthly', days=['Mon']), 'FREQ=MONTHLY')

    def test_projection_back_onto_the_old_fields(self):
        self.assertEqual(
            legacy_from_rrule('FREQ=WEEKLY;BYDAY=MO,WE'),
            {'is_recurring': True, 'recurrence_cadence': 'weekly', 'recurrence_days': ['Mon', 'Wed']},
        )

    def test_a_rule_the_old_shape_cannot_express_says_so(self):
        """"Repeats, and the old fields decline to say how" is the honest
        answer. Storing 'weekly' for an every-other-week rule would not be."""
        for rule in ('FREQ=WEEKLY;INTERVAL=2', 'FREQ=YEARLY'):
            projected = legacy_from_rrule(rule)
            self.assertTrue(projected['is_recurring'], rule)
            self.assertEqual(projected['recurrence_cadence'], '', rule)


class EventRecurrenceApiTests(TwoCompanyTestCase):
    def post_event(self, **body):
        payload = {'title': 'Standup', 'start_at': FUTURE_START_AT, 'audience': 'personal'}
        payload.update(body)
        return self.client.post(
            '/api/v1/events/', json.dumps(payload), content_type='application/json',
            **auth_header(self.owner_a),
        )

    def patch_event(self, event, **body):
        return self.client.patch(
            f"/api/v1/events/{event['id']}/", json.dumps(body),
            content_type='application/json', **auth_header(self.owner_a),
        )

    def test_an_explicit_rule_is_stored_and_returned(self):
        response = self.post_event(recurrence_rule='FREQ=WEEKLY;BYDAY=TU')
        self.assertEqual(response.status_code, 201)
        event = response.json()['data']['event']
        self.assertEqual(event['recurrence_rule'], 'FREQ=WEEKLY;BYDAY=TU')
        # The deprecated fields are derived, not stored in parallel.
        self.assertTrue(event['is_recurring'])
        self.assertEqual(event['recurrence_cadence'], 'weekly')
        self.assertEqual(event['recurrence_days'], ['Tue'])

    def test_a_legacy_client_still_works(self):
        response = self.post_event(
            is_recurring=True, recurrence_cadence='weekly', recurrence_days=['Mon', 'Fri'],
        )
        self.assertEqual(response.status_code, 201)
        event = response.json()['data']['event']
        self.assertEqual(event['recurrence_rule'], 'FREQ=WEEKLY;BYDAY=MO,FR')

    def test_an_explicit_rule_wins_over_the_legacy_fields(self):
        response = self.post_event(
            recurrence_rule='FREQ=MONTHLY', is_recurring=True, recurrence_cadence='daily',
        )
        self.assertEqual(response.json()['data']['event']['recurrence_rule'], 'FREQ=MONTHLY')

    def test_an_invalid_rule_is_refused(self):
        response = self.post_event(recurrence_rule='every other tuesday')
        self.assertEqual(response.status_code, 400)
        self.assertIn('invalid_recurrence_rule', response.json()['errors'])
        self.assertFalse(Event.objects.filter(title='Standup').exists())

    def test_an_unrelated_edit_does_not_clear_the_rule(self):
        event = self.post_event(recurrence_rule='FREQ=DAILY').json()['data']['event']
        self.assertEqual(self.patch_event(event, title='Renamed').status_code, 200)
        self.assertEqual(Event.objects.get(id=event['id']).recurrence_rule, 'FREQ=DAILY')

    def test_an_empty_rule_clears_the_recurrence(self):
        event = self.post_event(recurrence_rule='FREQ=DAILY').json()['data']['event']
        self.assertEqual(self.patch_event(event, recurrence_rule='').status_code, 200)
        stored = Event.objects.get(id=event['id'])
        self.assertEqual(stored.recurrence_rule, '')
        self.assertFalse(stored.is_recurring)

    def test_a_legacy_patch_still_reaches_the_rule(self):
        event = self.post_event().json()['data']['event']
        self.assertEqual(
            self.patch_event(event, is_recurring=True, recurrence_cadence='daily').status_code, 200,
        )
        self.assertEqual(Event.objects.get(id=event['id']).recurrence_rule, 'FREQ=DAILY')
