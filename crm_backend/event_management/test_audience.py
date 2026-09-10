"""Who an event is for -- creation, visibility, escalation, and the response.

Split from ``event_management/tests.py`` because it tests a different thing:
that file covers the event CRUD contract, this one covers the audience matrix
introduced in WP13 (§6). The two ends worth watching are ``personal``, which
has exactly one reader and no administrative override, and ``company``, which
only the Owner or a company manager may schedule.
"""

import json
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from audit.models import AuditAction, AuditEvent
from departments_and_teams.models import Department, Team
from django.apps import apps as django_apps
from django.utils import timezone
from users.models import CompanyUserProfile, User

from event_management.models import Event, EventAttendee

PASSWORD = 'Kx9#mQ2vLp8Z'
FUTURE_START_AT = (timezone.now() + timedelta(days=30)).isoformat()


class AudienceFixture(TwoCompanyTestCase):
    """One company with every role the matrix distinguishes, plus a second
    department and a team, so "their own department" and "their own team" are
    testable as the negative case too."""

    def setUp(self):
        super().setUp()
        self.sales = Department.objects.create(name='Sales', company=self.company_a)
        self.manager_a = self.make_member('cm-a', CompanyUserProfile.Role.COMPANY_MANAGER)
        self.lead_eng = self.make_member(
            'dl-eng', CompanyUserProfile.Role.DEPARTMENT_LEADER, department=self.department_a,
        )
        self.lead_sales = self.make_member(
            'dl-sales', CompanyUserProfile.Role.DEPARTMENT_LEADER, department=self.sales,
        )
        self.sales_member = self.make_member(
            'dm-sales', CompanyUserProfile.Role.DEPARTMENT_MEMBER, department=self.sales,
        )
        self.team_lead = self.make_member('team-lead', CompanyUserProfile.Role.DEPARTMENT_MEMBER)
        self.team = Team.objects.create(name='Platform', company=self.company_a, leader=self.team_lead)
        self.team.members.add(self.member_a)

    def make_member(self, handle, role, department=None):
        user = User.objects.create_user(
            email=f'{handle}@example.com', username=handle, password=PASSWORD,
        )
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, role=role, department=department,
        )
        return user

    def post_event(self, actor, **body):
        payload = {'title': 'Sync', 'start_at': FUTURE_START_AT}
        payload.update(body)
        return self.client.post(
            '/api/v1/events/', json.dumps(payload), content_type='application/json',
            **auth_header(actor),
        )

    def make_event(self, actor, **body):
        """Create through the API and return the event dict, asserting it
        worked -- so a fixture that stops being creatable fails loudly rather
        than as a KeyError three lines later."""
        response = self.post_event(actor, **body)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['data']['event']


class AudienceCreationTests(AudienceFixture):
    """Scheduling widens as the audience widens."""

    def test_any_member_may_create_a_personal_event(self):
        event = self.make_event(self.member_a, audience='personal')
        self.assertEqual(event['audience'], 'personal')

    def test_any_member_may_create_a_custom_meeting_with_attendees(self):
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)],
        )
        self.assertEqual(event['audience'], 'custom')
        self.assertEqual([a['id'] for a in event['attendees']], [str(self.sales_member.id)])

    def test_department_member_may_not_create_a_company_event(self):
        response = self.post_event(self.member_a, audience='company')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Event.objects.filter(audience='company').exists())

    def test_department_leader_may_not_create_a_company_event(self):
        self.assertEqual(self.post_event(self.lead_eng, audience='company').status_code, 403)

    def test_owner_and_company_manager_may_create_a_company_event(self):
        self.assertEqual(self.post_event(self.owner_a, audience='company').status_code, 201)
        self.assertEqual(self.post_event(self.manager_a, audience='company').status_code, 201)

    def test_department_leader_may_create_for_their_own_department_only(self):
        allowed = self.post_event(
            self.lead_eng, audience='department', department_id=str(self.department_a.id),
        )
        self.assertEqual(allowed.status_code, 201)
        refused = self.post_event(
            self.lead_eng, audience='department', department_id=str(self.sales.id),
        )
        self.assertEqual(refused.status_code, 403)

    def test_department_member_may_not_create_for_their_department(self):
        response = self.post_event(
            self.member_a, audience='department', department_id=str(self.department_a.id),
        )
        self.assertEqual(response.status_code, 403)

    def test_department_audience_must_name_a_department(self):
        response = self.post_event(self.owner_a, audience='department')
        self.assertEqual(response.status_code, 400)
        self.assertIn('audience_requires_department', response.json()['errors'])

    def test_team_lead_may_create_for_their_own_team(self):
        response = self.post_event(self.team_lead, audience='team', team_id=str(self.team.id))
        self.assertEqual(response.status_code, 201)

    def test_team_member_who_is_not_the_lead_may_not(self):
        response = self.post_event(self.member_a, audience='team', team_id=str(self.team.id))
        self.assertEqual(response.status_code, 403)

    def test_team_audience_must_name_a_team(self):
        response = self.post_event(self.owner_a, audience='team')
        self.assertEqual(response.status_code, 400)
        self.assertIn('audience_requires_team', response.json()['errors'])

    def test_personal_event_may_not_have_attendees(self):
        response = self.post_event(
            self.member_a, audience='personal', attendee_ids=[str(self.sales_member.id)],
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('personal_event_has_attendees', response.json()['errors'])

    def test_omitted_audience_defaults_to_custom(self):
        """The compatibility default. A client that predates the field sends a
        meeting with attendees, so it gets one -- not a company broadcast it
        was never entitled to, and not a personal entry that drops the people
        it named."""
        event = self.make_event(self.member_a, attendee_ids=[str(self.sales_member.id)])
        self.assertEqual(event['audience'], 'custom')

    def test_attendee_from_another_company_is_refused(self):
        response = self.post_event(
            self.member_a, audience='custom', attendee_ids=[str(self.owner_b.id)],
        )
        self.assertEqual(response.status_code, 400)


class AudienceVisibilityTests(AudienceFixture):
    """View follows the audience, plus the containment rule that anyone who
    may delete an event may also see it."""

    def visible_titles(self, actor):
        response = self.client.get('/api/v1/events/?page_size=100', **auth_header(actor))
        self.assertEqual(response.status_code, 200)
        return {row['title'] for row in response.json()['data']['results']}

    def can_get(self, actor, event):
        return self.client.get(f"/api/v1/events/{event['id']}/", **auth_header(actor)).status_code == 200

    def test_personal_event_is_visible_to_its_organizer_alone(self):
        event = self.make_event(self.member_a, audience='personal', title='Dentist')
        self.assertTrue(self.can_get(self.member_a, event))
        for outsider in (self.owner_a, self.manager_a, self.lead_eng, self.sales_member):
            self.assertFalse(self.can_get(outsider, event), f'{outsider.username} could read a personal event')
        self.assertNotIn('Dentist', self.visible_titles(self.owner_a))

    def test_personal_event_has_no_administrative_override(self):
        """Deliberately narrower than the brief's flat "Owner/CM may edit any
        event": the same rule documents.Document already applies to its
        personal scope. An Owner who cannot read an entry must not be able to
        delete it."""
        event = self.make_event(self.member_a, audience='personal')
        self.assertEqual(
            self.client.delete(f"/api/v1/events/{event['id']}/", **auth_header(self.owner_a)).status_code, 403,
        )
        self.assertFalse(Event.objects.get(id=event['id']).is_deleted)

    def test_company_event_is_visible_to_every_member(self):
        event = self.make_event(self.owner_a, audience='company', title='All Hands')
        for member in (self.member_a, self.sales_member, self.lead_sales, self.manager_a):
            self.assertTrue(self.can_get(member, event))
        self.assertIn('All Hands', self.visible_titles(self.sales_member))

    def test_department_event_is_visible_to_that_department_only(self):
        event = self.make_event(
            self.owner_a, audience='department', department_id=str(self.department_a.id),
            title='Eng Standup',
        )
        self.assertTrue(self.can_get(self.member_a, event))
        self.assertFalse(self.can_get(self.sales_member, event))
        self.assertNotIn('Eng Standup', self.visible_titles(self.sales_member))
        self.assertIn('Eng Standup', self.visible_titles(self.member_a))

    def test_department_leader_sees_events_naming_their_department(self):
        event = self.make_event(
            self.owner_a, audience='department', department_id=str(self.department_a.id),
            title='Eng Standup',
        )
        self.assertTrue(self.can_get(self.lead_eng, event))
        self.assertFalse(self.can_get(self.lead_sales, event))

    def test_team_event_is_visible_to_the_team_only(self):
        event = self.make_event(
            self.team_lead, audience='team', team_id=str(self.team.id), title='Platform Sync',
        )
        self.assertTrue(self.can_get(self.member_a, event))  # on the team
        self.assertTrue(self.can_get(self.team_lead, event))  # leads it
        self.assertFalse(self.can_get(self.sales_member, event))
        self.assertNotIn('Platform Sync', self.visible_titles(self.sales_member))

    def test_custom_event_is_visible_to_its_attendees(self):
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)], title='1:1',
        )
        self.assertTrue(self.can_get(self.sales_member, event))
        self.assertIn('1:1', self.visible_titles(self.sales_member))
        self.assertFalse(self.can_get(self.lead_sales, event))

    def test_custom_event_is_visible_to_the_owner_who_may_delete_it(self):
        """Not an audience match -- the containment rule. The brief puts
        Owner/CM on the edit list for every audience but personal, and a
        delete button on an unreadable row is not a coherent product."""
        event = self.make_event(self.member_a, audience='custom', title='1:1')
        self.assertTrue(self.can_get(self.owner_a, event))
        self.assertEqual(
            self.client.delete(f"/api/v1/events/{event['id']}/", **auth_header(self.owner_a)).status_code, 200,
        )

    def test_list_never_shows_another_companys_events(self):
        self.make_event(self.owner_a, audience='company', title='Ours')
        Event.objects.create(
            title='Theirs', company=self.company_b, organizer=self.owner_b,
            audience=Event.Audience.COMPANY, start_at=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(self.visible_titles(self.owner_a), {'Ours'})

    def test_cross_tenant_read_of_a_company_event_is_refused(self):
        event = self.make_event(self.owner_a, audience='company')
        self.assertFalse(self.can_get(self.owner_b, event))

    def test_a_deactivated_member_keeps_nothing_over_events_they_organized(self):
        """Organizing an event is a per-event grant, and WP3a settled that
        nothing per-event grants anything to a non-member. Deactivation does
        not touch Django auth, so the JWT still authenticates -- the check has
        to be membership first, organizer second."""
        event = self.make_event(self.member_a, audience='custom', title='Theirs')
        CompanyUserProfile.objects.filter(user=self.member_a, company=self.company_a).update(is_active=False)

        self.assertFalse(self.can_get(self.member_a, event))
        self.assertEqual(
            self.client.delete(f"/api/v1/events/{event['id']}/", **auth_header(self.member_a)).status_code, 403,
        )
        self.assertFalse(Event.objects.get(id=event['id']).is_deleted)
        self.assertEqual(self.visible_titles(self.member_a), set())


class AudienceEscalationTests(AudienceFixture):
    """Changing the audience is checked against the creation matrix, not just
    against manage rights on the event as it stands."""

    def patch(self, actor, event, **body):
        return self.client.patch(
            f"/api/v1/events/{event['id']}/", json.dumps(body),
            content_type='application/json', **auth_header(actor),
        )

    def test_member_cannot_widen_their_own_event_to_company(self):
        event = self.make_event(self.member_a, audience='custom')
        response = self.patch(self.member_a, event, audience='company')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Event.objects.get(id=event['id']).audience, 'custom')

    def test_member_cannot_widen_their_own_event_to_their_department(self):
        event = self.make_event(self.member_a, audience='custom')
        response = self.patch(self.member_a, event, audience='department',
                              department_id=str(self.department_a.id))
        self.assertEqual(response.status_code, 403)

    def test_department_leader_may_widen_their_own_departments_event(self):
        event = self.make_event(self.lead_eng, audience='custom')
        response = self.patch(self.lead_eng, event, audience='department',
                              department_id=str(self.department_a.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Event.objects.get(id=event['id']).audience, 'department')

    def test_owner_may_widen_to_company(self):
        event = self.make_event(self.member_a, audience='custom')
        self.assertEqual(self.patch(self.owner_a, event, audience='company').status_code, 200)

    def test_widening_to_department_without_one_is_refused(self):
        event = self.make_event(self.owner_a, audience='custom')
        response = self.patch(self.owner_a, event, audience='department')
        self.assertEqual(response.status_code, 400)
        self.assertIn('audience_requires_department', response.json()['errors'])

    def test_narrowing_to_personal_while_attendees_remain_is_refused(self):
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)],
        )
        response = self.patch(self.member_a, event, audience='personal')
        self.assertEqual(response.status_code, 400)
        self.assertIn('personal_event_has_attendees', response.json()['errors'])

    def test_audience_change_is_audited(self):
        event = self.make_event(self.member_a, audience='custom')
        before = AuditEvent.objects.filter(action=AuditAction.EVENT_AUDIENCE_CHANGED).count()
        self.assertEqual(self.patch(self.owner_a, event, audience='company').status_code, 200)
        rows = AuditEvent.objects.filter(action=AuditAction.EVENT_AUDIENCE_CHANGED)
        self.assertEqual(rows.count(), before + 1)
        row = rows.order_by('-created_at').first()
        self.assertEqual(row.actor_id, self.owner_a.id)
        self.assertEqual(row.before, {'audience': 'custom'})
        self.assertEqual(row.after, {'audience': 'company'})

    def test_an_edit_that_does_not_touch_the_audience_writes_no_audit_row(self):
        event = self.make_event(self.member_a, audience='custom')
        before = AuditEvent.objects.filter(action=AuditAction.EVENT_AUDIENCE_CHANGED).count()
        self.assertEqual(self.patch(self.member_a, event, title='Renamed').status_code, 200)
        self.assertEqual(
            AuditEvent.objects.filter(action=AuditAction.EVENT_AUDIENCE_CHANGED).count(), before,
        )

    def test_cross_tenant_escalation_is_refused(self):
        event = self.make_event(self.owner_a, audience='company')
        response = self.patch(self.owner_b, event, audience='personal')
        self.assertEqual(response.status_code, 403)


class AttendeeResponseTests(AudienceFixture):
    def respond(self, actor, event, value):
        return self.client.post(
            f"/api/v1/events/{event['id']}/response/", json.dumps({'response': value}),
            content_type='application/json', **auth_header(actor),
        )

    def test_attendee_can_record_a_response(self):
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)],
        )
        response = self.respond(self.sales_member, event, 'accepted')
        self.assertEqual(response.status_code, 200)
        row = EventAttendee.objects.get(event_id=event['id'], user=self.sales_member)
        self.assertEqual(row.response, 'accepted')
        self.assertIsNotNone(row.responded_at)
        returned = response.json()['data']['event']['attendees'][0]
        self.assertEqual(returned['response'], 'accepted')

    def test_an_unanswered_invitation_is_distinguishable_from_a_declined_one(self):
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)],
        )
        row = EventAttendee.objects.get(event_id=event['id'], user=self.sales_member)
        self.assertEqual(row.response, 'no_response')
        self.assertIsNone(row.responded_at)

    def test_organizer_cannot_answer_on_an_attendees_behalf(self):
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)],
        )
        self.assertEqual(self.respond(self.member_a, event, 'accepted').status_code, 403)
        self.assertEqual(
            EventAttendee.objects.get(event_id=event['id'], user=self.sales_member).response, 'no_response',
        )

    def test_a_member_who_can_see_the_event_but_was_not_invited_cannot_respond(self):
        event = self.make_event(self.owner_a, audience='company')
        self.assertEqual(self.respond(self.member_a, event, 'accepted').status_code, 403)

    def test_cross_tenant_response_is_refused(self):
        event = self.make_event(self.owner_a, audience='company')
        self.assertEqual(self.respond(self.owner_b, event, 'accepted').status_code, 403)

    def test_editing_the_attendee_list_preserves_existing_responses(self):
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)],
        )
        self.respond(self.sales_member, event, 'accepted')
        self.client.patch(
            f"/api/v1/events/{event['id']}/",
            json.dumps({'attendee_ids': [str(self.sales_member.id), str(self.lead_sales.id)]}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertEqual(
            EventAttendee.objects.get(event_id=event['id'], user=self.sales_member).response, 'accepted',
        )
        self.assertEqual(
            EventAttendee.objects.get(event_id=event['id'], user=self.lead_sales).response, 'no_response',
        )

    def test_removing_an_attendee_removes_their_row_and_their_view(self):
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)],
        )
        self.client.patch(
            f"/api/v1/events/{event['id']}/", json.dumps({'attendee_ids': []}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertFalse(EventAttendee.objects.filter(event_id=event['id']).exists())
        self.assertEqual(
            self.client.get(f"/api/v1/events/{event['id']}/", **auth_header(self.sales_member)).status_code, 403,
        )

    def test_attendee_rows_are_dual_written_to_the_deprecated_m2m(self):
        """Kept for one release so a rollback does not lose who was invited.
        Delete this test with the field."""
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)],
        )
        stored = Event.objects.get(id=event['id'])
        self.assertEqual([u.id for u in stored.attendees.all()], [self.sales_member.id])


class AudienceBackfillTests(AudienceFixture):
    """Migration 0003 against the legacy shape.

    The data function is invoked directly against the live app registry rather
    than by winding the migration graph backwards, matching
    users/test_owner_profile_backfill.py -- rewinding would mutate the schema
    of whichever xdist worker ran it, and the logic is what is worth testing.
    """

    def setUp(self):
        super().setUp()
        from importlib import import_module

        self.forwards = import_module(
            'event_management.migrations.0003_backfill_event_audience_attendees'
        ).backfill

    def legacy_event(self, **fields):
        """An event as the previous release wrote one: no audience, attendees
        on the M2M, recurrence in the three old fields."""
        attendees = fields.pop('attendee_users', [])
        event = Event.objects.create(
            company=self.company_a, organizer=self.owner_a,
            start_at=timezone.now() + timedelta(days=5),
            **{'title': 'Legacy', 'audience': Event.Audience.PERSONAL, **fields},
        )
        if attendees:
            event.attendees.set(attendees)
        return event

    def test_existing_events_become_company_wide(self):
        event = self.legacy_event()
        self.forwards(django_apps, None)
        event.refresh_from_db()
        self.assertEqual(event.audience, 'company')
        # Which is what preserving current visibility means in practice.
        self.assertEqual(
            self.client.get(f'/api/v1/events/{event.id}/', **auth_header(self.sales_member)).status_code, 200,
        )

    def test_m2m_attendees_become_rows(self):
        event = self.legacy_event(attendee_users=[self.member_a, self.sales_member])
        self.forwards(django_apps, None)
        rows = EventAttendee.objects.filter(event=event)
        self.assertEqual({row.user_id for row in rows}, {self.member_a.id, self.sales_member.id})
        self.assertTrue(all(row.response == 'no_response' for row in rows))
        # The M2M never recorded who invited whom; the backfill declines to guess.
        self.assertTrue(all(row.invited_by_id is None for row in rows))

    def test_backfill_is_idempotent(self):
        event = self.legacy_event(attendee_users=[self.member_a])
        self.forwards(django_apps, None)
        self.forwards(django_apps, None)
        self.assertEqual(EventAttendee.objects.filter(event=event).count(), 1)

    def test_legacy_recurrence_becomes_an_rrule(self):
        weekly = self.legacy_event(
            is_recurring=True, recurrence_cadence='weekly', recurrence_days=['Wed', 'Mon'],
        )
        monthly = self.legacy_event(is_recurring=True, recurrence_cadence='monthly')
        flag_only = self.legacy_event(is_recurring=True, recurrence_cadence='')
        never = self.legacy_event()
        self.forwards(django_apps, None)
        weekly.refresh_from_db()
        monthly.refresh_from_db()
        flag_only.refresh_from_db()
        never.refresh_from_db()
        # Calendar order, not click order.
        self.assertEqual(weekly.recurrence_rule, 'FREQ=WEEKLY;BYDAY=MO,WE')
        self.assertEqual(monthly.recurrence_rule, 'FREQ=MONTHLY')
        # "Repeats, somehow" was never a rule.
        self.assertEqual(flag_only.recurrence_rule, '')
        self.assertEqual(never.recurrence_rule, '')


class ReportedManageFlagTests(AudienceFixture):
    """The server tells the client who may manage each event, so the client
    never re-derives the audience matrix and drifts (the mistake
    lib/projectPermissions.ts made for projects)."""

    def event_payload(self, actor, event):
        response = self.client.get(f"/api/v1/events/{event['id']}/", **auth_header(actor))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']['event']

    def test_the_organizer_is_told_they_may_manage(self):
        event = self.make_event(self.member_a, audience='custom')
        self.assertTrue(self.event_payload(self.member_a, event)['can_manage'])

    def test_an_attendee_who_cannot_manage_is_told_so(self):
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)],
        )
        self.assertFalse(self.event_payload(self.sales_member, event)['can_manage'])

    def test_the_owner_may_manage_a_shared_event(self):
        event = self.make_event(self.member_a, audience='custom')
        self.assertTrue(self.event_payload(self.owner_a, event)['can_manage'])

    def test_a_department_leader_may_manage_their_own_departments_event(self):
        event = self.make_event(
            self.owner_a, audience='department', department_id=str(self.department_a.id),
        )
        self.assertTrue(self.event_payload(self.lead_eng, event)['can_manage'])
        self.assertFalse(self.event_payload(self.member_a, event)['can_manage'])

    def test_the_flag_matches_what_delete_actually_does(self):
        """The whole point: if the flag says no, the endpoint must also say
        no, and vice versa. A flag that disagrees with the server is worse
        than no flag.

        Both actors are chosen so they can *view* the event -- an attendee and
        the company owner -- since a viewer is the only caller for whom the
        flag is even reachable."""
        event = self.make_event(
            self.member_a, audience='custom', attendee_ids=[str(self.sales_member.id)],
        )
        for actor, expected in ((self.sales_member, False), (self.owner_a, True)):
            flag = self.event_payload(actor, event)['can_manage']
            self.assertEqual(flag, expected, f'{actor.username} flag')
            status = self.client.delete(
                f"/api/v1/events/{event['id']}/", **auth_header(actor),
            ).status_code
            self.assertEqual(status == 200, expected, f'{actor.username}: flag={flag} status={status}')

    def test_the_list_reports_the_flag_per_row(self):
        self.make_event(self.owner_a, audience='company', title='Theirs')
        self.make_event(self.member_a, audience='personal', title='Mine')
        response = self.client.get('/api/v1/events/?page_size=100', **auth_header(self.member_a))
        rows = {row['title']: row['can_manage'] for row in response.json()['data']['results']}
        self.assertFalse(rows['Theirs'])   # company event, member is not admin
        self.assertTrue(rows['Mine'])      # their own personal entry
