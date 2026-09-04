"""AI to-do generation: authorization, window derivation, and the
re-validation Django performs on whatever the AI service returns.

Celery runs eagerly in tests (conftest.py), so POST /todos/generate/ executes
the whole worker body inline -- which is exactly what makes the re-validation
rules testable end to end here. The AI service itself is always stubbed at
the requests.post boundary; no test ever reaches a real provider.
"""

import json
from datetime import timedelta
from unittest.mock import patch

from ai_agent.models import AITodoGeneration
from api.tests import TwoCompanyTestCase, auth_header
from django.utils import timezone
from notifications_and_activity.models import Notification
from projects_and_tasks.models import Project, Task

from todos.models import TodoItem
from todos.services import user_today


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def ai_ok(todos):
    return FakeResponse(200, {
        'success': True, 'message': 'ok',
        'data': {'provider': 'stub', 'model': 'stub-1', 'todos': todos},
    })


class AITodoGenerationTestCase(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.today = user_today(self.owner_a)
        self.project_a = Project.objects.create(
            title='Website Revamp', company=self.company_a, created_by=self.owner_a,
            deadline=timezone.now() + timedelta(days=365),
        )
        self.task = Task.objects.create(
            project=self.project_a, title='Ship the landing page', created_by=self.owner_a,
            assigned_to=self.owner_a, deadline=timezone.now() + timedelta(days=30),
        )

    def generate(self, user=None, **body):
        payload = {'mode': 'today'}
        payload.update(body)
        return self.client.post(
            '/api/v1/todos/generate/', json.dumps(payload), content_type='application/json',
            **auth_header(user or self.owner_a),
        )

    def todo_payload(self, **overrides):
        body = {
            'task_id': str(self.task.id), 'sequence': 1, 'title': 'Draft the hero copy',
            'notes': '', 'due_date': self.today.isoformat(), 'estimated_minutes': 45,
        }
        body.update(overrides)
        return body


class GenerationAuthorizationTests(AITodoGenerationTestCase):
    def test_requires_authentication(self):
        response = self.client.post(
            '/api/v1/todos/generate/', json.dumps({'mode': 'today'}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)

    def test_cannot_generate_from_a_task_assigned_to_someone_else(self):
        other_task = Task.objects.create(
            project=self.project_a, title='Not mine', created_by=self.owner_a,
            assigned_to=self.member_a, deadline=timezone.now() + timedelta(days=30),
        )
        with patch('ai_agent.tasks_todos.requests.post') as post:
            response = self.generate(mode='task', task_id=str(other_task.id))
        self.assertEqual(response.status_code, 404)
        post.assert_not_called()
        self.assertFalse(AITodoGeneration.objects.exists())

    def test_cannot_generate_from_another_companys_task(self):
        project_b = Project.objects.create(
            title='Theirs', company=self.company_b, created_by=self.owner_b,
            deadline=timezone.now() + timedelta(days=365),
        )
        task_b = Task.objects.create(
            project=project_b, title='Their task', created_by=self.owner_b, assigned_to=self.owner_b,
            deadline=timezone.now() + timedelta(days=30),
        )
        with patch('ai_agent.tasks_todos.requests.post') as post:
            response = self.generate(mode='task', task_id=str(task_b.id))
        self.assertEqual(response.status_code, 404)
        post.assert_not_called()

    def test_generating_with_nothing_assigned_never_calls_the_provider(self):
        self.task.delete()
        with patch('ai_agent.tasks_todos.requests.post') as post:
            response = self.generate(mode='today')
        self.assertEqual(response.status_code, 400)
        post.assert_not_called()

    def test_another_user_cannot_read_or_dismiss_my_generation(self):
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([self.todo_payload()])):
            generation_id = self.generate().json()['data']['generation']['id']

        read = self.client.get(f'/api/v1/todos/generations/{generation_id}/', **auth_header(self.member_a))
        self.assertEqual(read.status_code, 404)

        dismiss = self.client.post(
            f'/api/v1/todos/generations/{generation_id}/dismiss/', **auth_header(self.member_a),
        )
        self.assertEqual(dismiss.status_code, 404)
        self.assertEqual(TodoItem.objects.filter(is_deleted=False).count(), 1)


class GenerationHappyPathTests(AITodoGenerationTestCase):
    def test_generated_todos_are_persisted_privately_to_the_requester(self):
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([
            self.todo_payload(sequence=1, title='Draft the hero copy'),
            self.todo_payload(sequence=2, title='Export the assets'),
        ])):
            response = self.generate()

        self.assertEqual(response.status_code, 202)
        generation = AITodoGeneration.objects.get()
        self.assertEqual(generation.status, AITodoGeneration.STATUS.COMPLETED)
        self.assertEqual(generation.todo_count, 2)
        self.assertEqual(generation.provider, 'stub')

        todos = TodoItem.objects.filter(is_deleted=False).order_by('position')
        self.assertEqual([t.title for t in todos], ['Draft the hero copy', 'Export the assets'])
        for todo in todos:
            self.assertEqual(todo.user_id, self.owner_a.id)
            self.assertEqual(todo.source, TodoItem.SOURCE.AI_GENERATED)
            self.assertEqual(todo.task_id, self.task.id)
            self.assertEqual(todo.task_title_snapshot, 'Ship the landing page')

        # Nobody else can see them.
        listing = self.client.get('/api/v1/todos/', **auth_header(self.member_a))
        self.assertEqual(listing.json()['data']['results'], [])

    def test_the_requester_is_notified_when_the_batch_is_ready(self):
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([self.todo_payload()])):
            self.generate()
        notifications = Notification.objects.filter(recipient=self.owner_a, type=Notification.Type.TODOS_GENERATED)
        self.assertEqual(notifications.count(), 1)
        # Private -- nobody else hears about it.
        self.assertFalse(Notification.objects.filter(recipient=self.member_a).exists())

    def test_generated_todos_append_after_what_the_user_already_has_that_day(self):
        self.client.post(
            '/api/v1/todos/', json.dumps({'title': 'My own item', 'due_date': self.today.isoformat()}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([self.todo_payload()])):
            self.generate()

        titles = list(
            TodoItem.objects.filter(is_deleted=False).order_by('position').values_list('title', flat=True)
        )
        self.assertEqual(titles, ['My own item', 'Draft the hero copy'])

    def test_today_mode_collapses_the_window_to_a_single_day(self):
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([self.todo_payload()])):
            self.generate(mode='today')
        generation = AITodoGeneration.objects.get()
        self.assertEqual(generation.window_start, self.today)
        self.assertEqual(generation.window_end, self.today)

    def test_task_mode_never_plans_past_the_tasks_own_deadline(self):
        self.task.deadline = timezone.now() + timedelta(days=2)
        self.task.save(update_fields=['deadline'])
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([self.todo_payload()])):
            self.generate(mode='task', task_id=str(self.task.id), days=14)
        generation = AITodoGeneration.objects.get()
        # 14 days was asked for, but the task is due in 2 -- the window is
        # clamped rather than scheduling work for after it is due.
        self.assertLessEqual(generation.window_end, self.today + timedelta(days=2))


class GenerationRevalidationTests(AITodoGenerationTestCase):
    """Django never trusts the AI service's own validation (Rule 9)."""

    def test_a_todo_referencing_an_unsent_task_is_dropped(self):
        other_task = Task.objects.create(
            project=self.project_a, title='Not sent', created_by=self.owner_a,
            assigned_to=self.member_a, deadline=timezone.now() + timedelta(days=30),
        )
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([
            self.todo_payload(title='Legit'),
            self.todo_payload(title='Smuggled', task_id=str(other_task.id)),
        ])):
            self.generate()
        titles = list(TodoItem.objects.values_list('title', flat=True))
        self.assertEqual(titles, ['Legit'])

    def test_a_todo_outside_the_window_is_dropped(self):
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([
            self.todo_payload(title='In window'),
            self.todo_payload(title='Way out', due_date=(self.today + timedelta(days=60)).isoformat()),
        ])):
            self.generate(mode='today')
        self.assertEqual(list(TodoItem.objects.values_list('title', flat=True)), ['In window'])

    def test_reassigning_the_task_mid_flight_produces_no_todos_at_all(self):
        """The assignment is re-checked at persist time, not just at request
        time -- the task can change hands while the job is queued."""
        def reassign_then_respond(*args, **kwargs):
            Task.objects.filter(id=self.task.id).update(assigned_to=self.member_a)
            return ai_ok([self.todo_payload()])

        with patch('ai_agent.tasks_todos.requests.post', side_effect=reassign_then_respond):
            self.generate()

        self.assertFalse(TodoItem.objects.exists())
        generation = AITodoGeneration.objects.get()
        self.assertEqual(generation.status, AITodoGeneration.STATUS.FAILED)

    def test_more_todos_than_the_cap_are_truncated_to_the_cap(self):
        many = [self.todo_payload(sequence=i + 1, title=f'Step {i}') for i in range(10)]
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok(many)):
            self.generate(max_todos=3)
        self.assertEqual(TodoItem.objects.count(), 3)

    def test_an_empty_result_fails_the_generation_rather_than_reporting_success(self):
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([])):
            self.generate()
        generation = AITodoGeneration.objects.get()
        self.assertEqual(generation.status, AITodoGeneration.STATUS.FAILED)
        self.assertFalse(TodoItem.objects.exists())

    def test_a_todo_with_a_blank_title_is_dropped(self):
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([
            self.todo_payload(title='Real'),
            self.todo_payload(title='   '),
        ])):
            self.generate()
        self.assertEqual(list(TodoItem.objects.values_list('title', flat=True)), ['Real'])


class GenerationFailureTests(AITodoGenerationTestCase):
    def test_a_rejected_request_fails_the_generation_and_notifies(self):
        with patch(
            'ai_agent.tasks_todos.requests.post',
            return_value=FakeResponse(422, {'success': False, 'message': 'invalid output'}),
        ):
            self.generate()
        generation = AITodoGeneration.objects.get()
        self.assertEqual(generation.status, AITodoGeneration.STATUS.FAILED)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.owner_a, type=Notification.Type.TODOS_GENERATION_FAILED,
            ).exists()
        )

    def test_a_failure_message_never_leaks_the_provider_response_to_the_client(self):
        with patch(
            'ai_agent.tasks_todos.requests.post',
            return_value=FakeResponse(400, {'message': 'sk-live-secret-key-leaked'}),
        ):
            generation_id = self.generate().json()['data']['generation']['id']

        # The raw provider body is recorded server-side for diagnosis...
        self.assertIn('sk-live-secret', AITodoGeneration.objects.get().error_message)
        # ...but the user-facing failure notification carries none of it.
        notification = Notification.objects.get(type=Notification.Type.TODOS_GENERATION_FAILED)
        self.assertNotIn('sk-live-secret', notification.title + notification.message)
        self.assertIsNotNone(generation_id)


class GenerationConcurrencyTests(AITodoGenerationTestCase):
    def test_a_second_request_while_one_is_running_is_refused(self):
        AITodoGeneration.objects.create(
            user=self.owner_a, company=self.company_a, mode=AITodoGeneration.MODE.TODAY,
            source_task_ids=[str(self.task.id)], window_start=self.today, window_end=self.today,
            status=AITodoGeneration.STATUS.PROCESSING,
        )
        with patch('ai_agent.tasks_todos.requests.post') as post:
            response = self.generate()
        self.assertEqual(response.status_code, 409)
        post.assert_not_called()
        self.assertEqual(AITodoGeneration.objects.count(), 1)

    def test_someone_elses_running_generation_does_not_block_mine(self):
        AITodoGeneration.objects.create(
            user=self.member_a, company=self.company_a, mode=AITodoGeneration.MODE.TODAY,
            source_task_ids=[], window_start=self.today, window_end=self.today,
            status=AITodoGeneration.STATUS.PROCESSING,
        )
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([self.todo_payload()])):
            response = self.generate()
        self.assertEqual(response.status_code, 202)

    def test_reprocessing_the_same_generation_creates_no_duplicate_todos(self):
        """A Celery task can be delivered twice; the second run must be a
        no-op rather than a second copy of the checklist (Rule 8)."""
        from ai_agent.tasks_todos import process_todo_generation

        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([self.todo_payload()])):
            self.generate()
        generation = AITodoGeneration.objects.get()
        self.assertEqual(TodoItem.objects.count(), 1)

        # Force it back to a non-terminal state and redeliver.
        AITodoGeneration.objects.filter(id=generation.id).update(status=AITodoGeneration.STATUS.PROCESSING)
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([self.todo_payload()])):
            process_todo_generation(str(generation.id))
        self.assertEqual(TodoItem.objects.count(), 1)


class GenerationDismissTests(AITodoGenerationTestCase):
    def test_dismissing_removes_the_open_items_but_keeps_completed_work(self):
        with patch('ai_agent.tasks_todos.requests.post', return_value=ai_ok([
            self.todo_payload(sequence=1, title='Kept'),
            self.todo_payload(sequence=2, title='Dropped'),
        ])):
            generation_id = self.generate().json()['data']['generation']['id']

        kept = TodoItem.objects.get(title='Kept')
        self.client.patch(
            f'/api/v1/todos/{kept.id}/', json.dumps({'is_done': True}),
            content_type='application/json', **auth_header(self.owner_a),
        )

        response = self.client.post(
            f'/api/v1/todos/generations/{generation_id}/dismiss/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['dismissed'], 1)

        # The completed one survives: the owner did that work.
        self.assertFalse(TodoItem.objects.get(title='Kept').is_deleted)
        self.assertTrue(TodoItem.objects.get(title='Dropped').is_deleted)
