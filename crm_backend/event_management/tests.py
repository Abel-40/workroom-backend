"""Event/event-type API: happy path, auth, cross-tenant rejection, role
authorization, validation, default-type idempotency, and filtering --
reusing the TwoCompanyTestCase fixture (api/tests.py) so isolation is
checked the same way every other endpoint's tests check it.
"""

import json
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from django.utils import timezone
from users.models import CompanyUserProfile, User

from event_management.models import DefaultEventType, Event, EventType

# A fixed future-looking literal (e.g. '2026-06-01') silently turns into a
# past date -- and starts failing the create_event "no backdating" rule --
# the moment the real clock catches up to it. Always relative to "now" instead.
FUTURE_START_AT = (timezone.now() + timedelta(days=30)).isoformat()


class EventCrudTests(TwoCompanyTestCase):
    def create_event(self, owner=None, **overrides):
        body = {'title': 'All Hands', 'start_at': FUTURE_START_AT}
        body.update(overrides)
        return self.client.post(
            '/api/events/', json.dumps(body), content_type='application/json',
            **auth_header(owner or self.owner_a),
        )

    def test_happy_path_create(self):
        response = self.create_event()
        self.assertEqual(response.status_code, 201)
        event = response.json()['data']['event']
        self.assertEqual(event['title'], 'All Hands')
        self.assertEqual(event['organizer_id'], str(self.owner_a.id))
        stored = Event.objects.get(id=event['id'])
        self.assertEqual(stored.company_id, self.company_a.id)

    def test_requires_authentication(self):
        response = self.client.post(
            '/api/events/', json.dumps({'title': 'x', 'start_at': FUTURE_START_AT}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)

    def test_create_rejects_missing_start_at(self):
        response = self.client.post(
            '/api/events/', json.dumps({'title': 'No start'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 422)

    def test_create_rejects_past_start_at(self):
        response = self.create_event(start_at=(timezone.now() - timedelta(days=1)).isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Event.objects.filter(title='All Hands').exists())

    def test_create_rejects_department_from_another_company(self):
        from departments_and_teams.models import Department
        other_department = Department.objects.create(name='Other', company=self.company_b)
        response = self.create_event(department_id=str(other_department.id))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Event.objects.filter(title='All Hands').exists())

    def test_create_rejects_event_type_from_another_company(self):
        foreign_type = EventType.objects.create(name='Foreign', company=self.company_b)
        response = self.create_event(event_type_id=str(foreign_type.id))
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_attendee_from_another_company(self):
        response = self.create_event(attendee_ids=[str(self.owner_b.id)])
        self.assertEqual(response.status_code, 400)

    def test_create_accepts_valid_attendee(self):
        response = self.create_event(attendee_ids=[str(self.member_a.id)])
        self.assertEqual(response.status_code, 201)
        event = response.json()['data']['event']
        self.assertEqual([a['id'] for a in event['attendees']], [str(self.member_a.id)])

    def test_get_event_cross_tenant_rejected(self):
        # Matches the Project precedent (api/tests.py::test_private_project_hidden_
        # from_non_collaborator_company_member): a caller with no membership in the
        # resource's company gets 403 ("forbidden"), not 404 -- the row exists, they
        # simply aren't authorized to see it. Either status fully blocks cross-tenant
        # access; 403 is this codebase's established convention for that case.
        event = self.create_event().json()['data']['event']
        response = self.client.get(f"/api/events/{event['id']}/", **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)

    def test_get_event_unknown_id_is_404(self):
        response = self.client.get(
            '/api/events/00000000-0000-0000-0000-000000000000/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 404)

    def test_update_event_cross_tenant_rejected(self):
        event = self.create_event().json()['data']['event']
        response = self.client.patch(
            f"/api/events/{event['id']}/", json.dumps({'title': 'Hijacked'}),
            content_type='application/json', **auth_header(self.owner_b),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Event.objects.get(id=event['id']).title, 'All Hands')

    def test_member_can_create_but_not_delete_another_organizers_event(self):
        event = self.create_event(owner=self.owner_a).json()['data']['event']
        response = self.client.delete(f"/api/events/{event['id']}/", **auth_header(self.member_a))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Event.objects.get(id=event['id']).is_deleted)

    def test_organizer_can_delete_their_own_event(self):
        event = self.create_event(owner=self.member_a).json()['data']['event']
        response = self.client.delete(f"/api/events/{event['id']}/", **auth_header(self.member_a))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Event.objects.get(id=event['id']).is_deleted)

    def test_owner_can_delete_any_company_event(self):
        event = self.create_event(owner=self.member_a).json()['data']['event']
        response = self.client.delete(f"/api/events/{event['id']}/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)

    def test_department_leader_can_manage_only_their_own_department(self):
        from departments_and_teams.models import Department

        dl = User.objects.create_user(email='dl-events@example.com', username='dl-events', password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(
            user=dl, company=self.company_a, department=self.department_a,
            role=CompanyUserProfile.Role.DEPARTMENT_LEADER,
        )
        other_department = Department.objects.create(name='Sales', company=self.company_a)

        own_dept_event = self.create_event(owner=self.owner_a, department_id=str(self.department_a.id)).json()['data']['event']
        other_dept_event = self.create_event(owner=self.owner_a, department_id=str(other_department.id)).json()['data']['event']

        self.assertEqual(
            self.client.delete(f"/api/events/{own_dept_event['id']}/", **auth_header(dl)).status_code, 200,
        )
        self.assertEqual(
            self.client.delete(f"/api/events/{other_dept_event['id']}/", **auth_header(dl)).status_code, 403,
        )


class EventFilterAndPaginationTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.meeting_type = EventType.objects.create(name='Meeting', company=self.company_a)
        self.social_type = EventType.objects.create(name='Social', company=self.company_a)
        for i in range(3):
            Event.objects.create(
                title=f'Meeting {i}', company=self.company_a, event_type=self.meeting_type,
                department=self.department_a, organizer=self.owner_a, start_at=f'2026-0{i + 1}-01T10:00:00Z',
            )
        Event.objects.create(
            title='Party', company=self.company_a, event_type=self.social_type,
            organizer=self.owner_a, start_at='2026-05-01T10:00:00Z',
        )
        Event.objects.create(title='Other company event', company=self.company_b, organizer=self.owner_b, start_at='2026-01-01T10:00:00Z')

    def test_list_scoped_to_own_company(self):
        response = self.client.get('/api/events/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        titles = {e['title'] for e in response.json()['data']['results']}
        self.assertEqual(titles, {'Meeting 0', 'Meeting 1', 'Meeting 2', 'Party'})

    def test_filter_by_event_type(self):
        response = self.client.get(
            f'/api/events/?event_type_id={self.social_type.id}', **auth_header(self.owner_a),
        )
        titles = {e['title'] for e in response.json()['data']['results']}
        self.assertEqual(titles, {'Party'})

    def test_filter_by_department(self):
        response = self.client.get(
            f'/api/events/?department_id={self.department_a.id}', **auth_header(self.owner_a),
        )
        titles = {e['title'] for e in response.json()['data']['results']}
        self.assertEqual(titles, {'Meeting 0', 'Meeting 1', 'Meeting 2'})

    def test_filter_by_date_range(self):
        # Both bounds are inclusive (start_at__date__gte/lte) -- 2026-03-01
        # (Meeting 2's date) is the end_date itself, so it's correctly included.
        response = self.client.get(
            '/api/events/?start_date=2026-02-01&end_date=2026-03-01', **auth_header(self.owner_a),
        )
        titles = {e['title'] for e in response.json()['data']['results']}
        self.assertEqual(titles, {'Meeting 1', 'Meeting 2'})

    def test_pagination(self):
        response = self.client.get('/api/events/?page=1&page_size=2', **auth_header(self.owner_a))
        data = response.json()['data']
        self.assertEqual(len(data['results']), 2)
        self.assertEqual(data['meta']['count'], 4)
        self.assertTrue(data['meta']['has_next'])


class EventTypeConfigTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.default = DefaultEventType.objects.create(name='Meeting', sector=None)

    def list_config(self, user=None):
        return self.client.get('/api/company/default-config/', **auth_header(user or self.owner_a))

    def enable(self, selected_ids=None, use_all=False, user=None):
        body = {'use_all': use_all}
        if selected_ids is not None:
            body['selected_ids'] = [str(i) for i in selected_ids]
        return self.client.post(
            '/api/company/default-config/event-types/', json.dumps(body),
            content_type='application/json', **auth_header(user or self.owner_a),
        )

    def test_enabling_a_default_event_type_creates_a_traceable_row(self):
        response = self.enable(selected_ids=[self.default.id])
        self.assertEqual(response.status_code, 201)
        event_type = EventType.objects.get(company=self.company_a, name='Meeting')
        self.assertEqual(event_type.default_event_type_id, self.default.id)
        status_response = self.list_config()
        by_name = {t['name']: t for t in status_response.json()['data']['event_types']}
        self.assertTrue(by_name['Meeting']['enabled'])

    def test_enabling_twice_does_not_duplicate(self):
        self.enable(selected_ids=[self.default.id])
        response = self.enable(selected_ids=[self.default.id])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['data']['total_created'], 0)
        self.assertEqual(EventType.objects.filter(company=self.company_a, name='Meeting').count(), 1)

    def test_use_all_enables_every_available_default(self):
        DefaultEventType.objects.create(name='Training', sector=None)
        response = self.enable(use_all=True)
        self.assertEqual(response.json()['data']['total_created'], 2)

    def test_department_member_cannot_enable_defaults(self):
        response = self.enable(selected_ids=[self.default.id], user=self.member_a)
        self.assertEqual(response.status_code, 403)

    def test_create_custom_event_type(self):
        response = self.client.post(
            '/api/event-types/', json.dumps({'name': 'Hackathon', 'description': 'Internal hackathon'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(EventType.objects.filter(company=self.company_a, name='Hackathon', default_event_type__isnull=True).exists())

    def test_create_custom_event_type_rejects_duplicate_name(self):
        EventType.objects.create(name='Hackathon', company=self.company_a)
        response = self.client.post(
            '/api/event-types/', json.dumps({'name': 'Hackathon'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_department_member_cannot_create_custom_event_type(self):
        response = self.client.post(
            '/api/event-types/', json.dumps({'name': 'Hackathon'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 403)

    def test_list_event_types_scoped_to_own_company(self):
        EventType.objects.create(name='Mine', company=self.company_a)
        EventType.objects.create(name='Theirs', company=self.company_b)
        response = self.client.get('/api/event-types/', **auth_header(self.owner_a))
        names = {t['name'] for t in response.json()['data']['results']}
        self.assertEqual(names, {'Mine'})
