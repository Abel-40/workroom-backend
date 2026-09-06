"""Personal to-do API tests.

The highest-value tests here are the privacy ones: a todo is the only object
in Workroom that not even the company Owner may read, so "another user
cannot see, edit, or delete it" is the core business rule, not an edge case.
Reuses TwoCompanyTestCase (api/tests.py) so cross-tenant rejection is checked
the same way as every other endpoint.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from api.tests import TwoCompanyTestCase, auth_header
from django.utils import timezone
from projects_and_tasks.models import Project, Task
from users.models import CompanyUserProfile, User

from todos.models import TodoItem
from todos.services import user_today


class TodoTestCase(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.today = user_today(self.owner_a)
        self.project_a = Project.objects.create(
            title='Website Revamp', company=self.company_a, created_by=self.owner_a,
            deadline=timezone.now() + timedelta(days=365),
        )

    def create_todo(self, user=None, **overrides):
        body = {'title': 'Draft the brief', 'due_date': self.today.isoformat()}
        body.update(overrides)
        response = self.client.post(
            '/api/v1/todos/', json.dumps(body), content_type='application/json',
            **auth_header(user or self.owner_a),
        )
        return response

    def create_task(self, assigned_to=None, title='Ship the landing page'):
        return Task.objects.create(
            project=self.project_a, title=title, created_by=self.owner_a, assigned_to=assigned_to,
            deadline=timezone.now() + timedelta(days=30),
        )


class TodoPrivacyTests(TodoTestCase):
    """No role widens access to someone else's todos -- not Owner, not a
    Company Manager, not a Department Leader."""

    def setUp(self):
        super().setUp()
        self.response = self.create_todo(user=self.member_a, title='Private note')
        self.todo_id = self.response.json()['data']['todo']['id']

    def test_owner_of_the_company_cannot_list_a_members_todos(self):
        listing = self.client.get('/api/v1/todos/', **auth_header(self.owner_a))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()['data']['results'], [])

    def test_owner_of_the_company_cannot_read_edit_or_delete_a_members_todo(self):
        patch_response = self.client.patch(
            f'/api/v1/todos/{self.todo_id}/', json.dumps({'title': 'Hijacked'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(patch_response.status_code, 404)

        delete_response = self.client.delete(f'/api/v1/todos/{self.todo_id}/', **auth_header(self.owner_a))
        self.assertEqual(delete_response.status_code, 404)

        self.assertEqual(TodoItem.objects.get(id=self.todo_id).title, 'Private note')

    def test_another_company_cannot_touch_it_either(self):
        response = self.client.patch(
            f'/api/v1/todos/{self.todo_id}/', json.dumps({'is_done': True}),
            content_type='application/json', **auth_header(self.owner_b),
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(TodoItem.objects.get(id=self.todo_id).is_done)

    def test_a_foreign_todo_is_indistinguishable_from_a_nonexistent_one(self):
        """Both must be 404, never 403 -- a 403 would confirm the id exists."""
        real = self.client.delete(f'/api/v1/todos/{self.todo_id}/', **auth_header(self.owner_a))
        fake = self.client.delete(
            '/api/v1/todos/2f8f4b1e-0000-4000-8000-000000000000/', **auth_header(self.owner_a),
        )
        self.assertEqual(real.status_code, 404)
        self.assertEqual(fake.status_code, 404)
        self.assertEqual(real.json()['message'], fake.json()['message'])

    def test_a_todo_never_reaches_the_company_activity_feed(self):
        from notifications_and_activity.models import CompanyActivity

        self.assertFalse(CompanyActivity.objects.filter(company=self.company_a).exists())


class TodoCreationTests(TodoTestCase):
    def test_creating_a_todo_requires_a_day(self):
        response = self.client.post(
            '/api/v1/todos/', json.dumps({'title': 'No day picked'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(TodoItem.objects.filter(title='No day picked').exists())

    def test_a_due_date_absurdly_far_out_is_rejected(self):
        far = (self.today + timedelta(days=365 * 6)).isoformat()
        response = self.create_todo(due_date=far)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TodoItem.objects.exists())

    def test_a_past_due_date_is_allowed_because_overdue_is_a_real_state(self):
        response = self.create_todo(due_date=(self.today - timedelta(days=3)).isoformat())
        self.assertEqual(response.status_code, 201)

    def test_new_todos_append_to_the_end_of_their_day(self):
        first = self.create_todo(title='First').json()['data']['todo']
        second = self.create_todo(title='Second').json()['data']['todo']
        self.assertEqual(first['position'], 0)
        self.assertEqual(second['position'], 1)

    def test_title_is_trimmed_and_stored_without_surrounding_whitespace(self):
        response = self.create_todo(title='   Padded title   ')
        self.assertEqual(response.json()['data']['todo']['title'], 'Padded title')


class TodoTaskLinkTests(TodoTestCase):
    """A todo may only be built from work actually assigned to the caller."""

    def test_can_link_a_todo_to_a_task_assigned_to_me(self):
        task = self.create_task(assigned_to=self.owner_a)
        response = self.create_todo(task_id=str(task.id))
        self.assertEqual(response.status_code, 201)
        todo = response.json()['data']['todo']
        self.assertEqual(todo['task_id'], str(task.id))
        self.assertEqual(todo['task_title'], 'Ship the landing page')

    def test_cannot_link_to_a_task_assigned_to_someone_else(self):
        task = self.create_task(assigned_to=self.member_a)
        response = self.create_todo(task_id=str(task.id))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(TodoItem.objects.exists())

    def test_cannot_link_to_an_unassigned_task_even_when_visible(self):
        """owner_a can see this task (it's their own company's project) but
        it is assigned to nobody -- visibility is not the test here."""
        task = self.create_task(assigned_to=None)
        response = self.create_todo(task_id=str(task.id))
        self.assertEqual(response.status_code, 404)

    def test_cannot_link_to_another_companys_task(self):
        project_b = Project.objects.create(
            title='Their project', company=self.company_b, created_by=self.owner_b,
            deadline=timezone.now() + timedelta(days=365),
        )
        task = Task.objects.create(
            project=project_b, title='Their task', created_by=self.owner_b, assigned_to=self.owner_b,
            deadline=timezone.now() + timedelta(days=30),
        )
        response = self.create_todo(task_id=str(task.id))
        self.assertEqual(response.status_code, 404)

    def test_reassigning_the_task_away_revokes_the_live_link_but_keeps_the_note(self):
        task = self.create_task(assigned_to=self.owner_a)
        self.create_todo(task_id=str(task.id))

        task.assigned_to = self.member_a
        task.save(update_fields=['assigned_to'])

        listing = self.client.get('/api/v1/todos/', **auth_header(self.owner_a))
        todo = listing.json()['data']['results'][0]
        # The owner keeps their own private note and can still tell what it
        # was about, but the task's current state is no longer exposed.
        self.assertIsNone(todo['task_id'])
        self.assertNotIn('task_status', todo)
        self.assertEqual(todo['task_title'], 'Ship the landing page')


class TodoOrderingTests(TodoTestCase):
    """"Nearest first" is the whole point of the screen."""

    def test_todos_come_back_nearest_day_first_with_overdue_at_the_top(self):
        self.create_todo(title='Next week', due_date=(self.today + timedelta(days=7)).isoformat())
        self.create_todo(title='Today', due_date=self.today.isoformat())
        self.create_todo(title='Overdue', due_date=(self.today - timedelta(days=2)).isoformat())
        self.create_todo(title='Tomorrow', due_date=(self.today + timedelta(days=1)).isoformat())

        listing = self.client.get('/api/v1/todos/', **auth_header(self.owner_a))
        titles = [t['title'] for t in listing.json()['data']['results']]
        self.assertEqual(titles, ['Overdue', 'Today', 'Tomorrow', 'Next week'])

    def test_within_one_day_manual_position_decides_the_order(self):
        first = self.create_todo(title='First').json()['data']['todo']
        self.create_todo(title='Second')
        self.client.patch(
            f"/api/v1/todos/{first['id']}/", json.dumps({'position': 5}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        listing = self.client.get('/api/v1/todos/', **auth_header(self.owner_a))
        self.assertEqual([t['title'] for t in listing.json()['data']['results']], ['Second', 'First'])

    def test_completed_todos_are_hidden_unless_asked_for(self):
        todo = self.create_todo(title='Done thing').json()['data']['todo']
        self.client.patch(
            f"/api/v1/todos/{todo['id']}/", json.dumps({'is_done': True}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        hidden = self.client.get('/api/v1/todos/', **auth_header(self.owner_a))
        self.assertEqual(hidden.json()['data']['results'], [])

        shown = self.client.get('/api/v1/todos/?include_done=true', **auth_header(self.owner_a))
        self.assertEqual(len(shown.json()['data']['results']), 1)

    def test_scope_filters_narrow_to_the_right_days(self):
        self.create_todo(title='Overdue', due_date=(self.today - timedelta(days=1)).isoformat())
        self.create_todo(title='Today')
        self.create_todo(title='Later', due_date=(self.today + timedelta(days=3)).isoformat())

        for scope, expected in [
            ('overdue', ['Overdue']),
            ('today', ['Today']),
            ('upcoming', ['Later']),
            ('due', ['Overdue', 'Today']),
        ]:
            listing = self.client.get(f'/api/v1/todos/?scope={scope}', **auth_header(self.owner_a))
            self.assertEqual([t['title'] for t in listing.json()['data']['results']], expected, scope)


class TodoStateTransitionTests(TodoTestCase):
    def test_completing_and_reopening_maintains_completed_at(self):
        todo = self.create_todo().json()['data']['todo']
        done = self.client.patch(
            f"/api/v1/todos/{todo['id']}/", json.dumps({'is_done': True}),
            content_type='application/json', **auth_header(self.owner_a),
        ).json()['data']['todo']
        self.assertTrue(done['is_done'])
        self.assertIsNotNone(done['completed_at'])

        reopened = self.client.patch(
            f"/api/v1/todos/{todo['id']}/", json.dumps({'is_done': False}),
            content_type='application/json', **auth_header(self.owner_a),
        ).json()['data']['todo']
        self.assertFalse(reopened['is_done'])
        self.assertIsNone(reopened['completed_at'])

    def test_moving_a_todo_to_another_day_reassigns_its_position(self):
        self.create_todo(title='Sits on tomorrow', due_date=(self.today + timedelta(days=1)).isoformat())
        todo = self.create_todo(title='Moving').json()['data']['todo']
        self.assertEqual(todo['position'], 0)

        moved = self.client.patch(
            f"/api/v1/todos/{todo['id']}/", json.dumps({'due_date': (self.today + timedelta(days=1)).isoformat()}),
            content_type='application/json', **auth_header(self.owner_a),
        ).json()['data']['todo']
        # Appended to tomorrow's list rather than colliding at position 0.
        self.assertEqual(moved['position'], 1)

    def test_deleting_a_todo_is_a_soft_delete_and_hides_it(self):
        todo = self.create_todo().json()['data']['todo']
        response = self.client.delete(f"/api/v1/todos/{todo['id']}/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(TodoItem.objects.get(id=todo['id']).is_deleted)

        listing = self.client.get('/api/v1/todos/', **auth_header(self.owner_a))
        self.assertEqual(listing.json()['data']['results'], [])

    def test_an_update_cannot_reassign_ownership_or_tenant(self):
        """Protected fields aren't in the schema, so a client sending them is
        ignored rather than obeyed (Rule 11)."""
        todo = self.create_todo().json()['data']['todo']
        self.client.patch(
            f"/api/v1/todos/{todo['id']}/",
            json.dumps({'title': 'Renamed', 'user': str(self.member_a.id), 'company': str(self.company_b.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        row = TodoItem.objects.get(id=todo['id'])
        self.assertEqual(row.title, 'Renamed')
        self.assertEqual(row.user_id, self.owner_a.id)
        self.assertEqual(row.company_id, self.company_a.id)


class TodoSummaryTests(TodoTestCase):
    def test_summary_counts_only_the_callers_open_todos(self):
        self.create_todo(title='Overdue', due_date=(self.today - timedelta(days=1)).isoformat())
        self.create_todo(title='Today')
        self.create_todo(title='Later', due_date=(self.today + timedelta(days=2)).isoformat())
        self.create_todo(user=self.member_a, title='Not mine')

        done = self.create_todo(title='Finished').json()['data']['todo']
        self.client.patch(
            f"/api/v1/todos/{done['id']}/", json.dumps({'is_done': True}),
            content_type='application/json', **auth_header(self.owner_a),
        )

        summary = self.client.get('/api/v1/todos/summary/', **auth_header(self.owner_a)).json()['data']
        self.assertEqual(summary['overdue'], 1)
        self.assertEqual(summary['due_today'], 1)
        self.assertEqual(summary['upcoming'], 1)
        self.assertEqual(summary['open'], 3)


class TodoTimezoneTests(TodoTestCase):
    """"Today" must be the owner's today, not the server's."""

    def test_today_is_computed_in_the_users_own_timezone(self):
        user = User.objects.create_user(
            email='kiribati@example.com', username='kiribati', password='Kx9#mQ2vLp8Z',
        )
        user.timezone = 'Pacific/Kiritimati'  # UTC+14
        user.save(update_fields=['timezone'])
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, department=self.department_a,
            role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        honolulu = User.objects.create_user(
            email='honolulu@example.com', username='honolulu', password='Kx9#mQ2vLp8Z',
        )
        honolulu.timezone = 'Pacific/Honolulu'  # UTC-10
        honolulu.save(update_fields=['timezone'])

        # One instant, two different calendar days: 10:30Z is already
        # 2026-06-02 in Kiritimati (UTC+14) while Honolulu (UTC-10) is still
        # on 2026-06-01. A server-side date would hand both the same answer,
        # and it would be the wrong one for at least one of them.
        frozen = datetime(2026, 6, 1, 10, 30, tzinfo=UTC)
        with patch('django.utils.timezone.now', return_value=frozen):
            self.assertEqual(user_today(user).isoformat(), '2026-06-02')
            self.assertEqual(user_today(honolulu).isoformat(), '2026-06-01')

    def test_an_unparseable_stored_timezone_falls_back_instead_of_erroring(self):
        user = User.objects.create_user(
            email='drifted@example.com', username='drifted', password='Kx9#mQ2vLp8Z',
        )
        user.timezone = 'Not/AZone'
        user.save(update_fields=['timezone'])
        self.assertIsNotNone(user_today(user))


class AssignedTasksEndpointTests(TodoTestCase):
    """The source list the to-do screen and its AI generation build from."""

    def test_returns_only_tasks_assigned_to_the_caller(self):
        mine = self.create_task(assigned_to=self.owner_a, title='Mine')
        self.create_task(assigned_to=self.member_a, title='Theirs')
        self.create_task(assigned_to=None, title='Nobody')

        response = self.client.get('/api/v1/tasks/assigned-to-me/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        results = response.json()['data']['results']
        self.assertEqual([t['id'] for t in results], [str(mine.id)])
        self.assertEqual(results[0]['project_title'], 'Website Revamp')

    def test_completed_tasks_are_excluded_by_default(self):
        task = self.create_task(assigned_to=self.owner_a, title='Finished')
        task.status = Task.STATUS.DONE
        task.save(update_fields=['status'])

        default = self.client.get('/api/v1/tasks/assigned-to-me/', **auth_header(self.owner_a))
        self.assertEqual(default.json()['data']['results'], [])

        everything = self.client.get(
            '/api/v1/tasks/assigned-to-me/?open_only=false', **auth_header(self.owner_a),
        )
        self.assertEqual(len(everything.json()['data']['results']), 1)

    def test_another_companys_assigned_tasks_never_appear(self):
        project_b = Project.objects.create(
            title='Their project', company=self.company_b, created_by=self.owner_b,
            deadline=timezone.now() + timedelta(days=365),
        )
        Task.objects.create(
            project=project_b, title='Their task', created_by=self.owner_b, assigned_to=self.owner_b,
            deadline=timezone.now() + timedelta(days=30),
        )
        response = self.client.get('/api/v1/tasks/assigned-to-me/', **auth_header(self.owner_a))
        self.assertEqual(response.json()['data']['results'], [])

    def test_requires_authentication(self):
        response = self.client.get('/api/v1/tasks/assigned-to-me/')
        self.assertEqual(response.status_code, 401)
