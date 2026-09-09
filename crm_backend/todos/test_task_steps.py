""""Break this into steps" -- a task you hold, turned into a private checklist.

This is where most of the pressure to let anybody create tasks was actually
coming from. "I need to track the three things this task breaks into" is a real
need, and answering it by widening task creation would have put somebody's
personal working notes on the company board, counted them toward project
progress, and made them assignable to other people.

To-dos are already the right home: private to one person, invisible to every
role including the company Owner, and outside analytics. So the steps are
to-dos, and the privacy rules that apply to every other to-do apply here
unchanged.
"""

import json
from datetime import timedelta

from api.tests import auth_header
from django.utils import timezone
from projects_and_tasks.models import Task
from users.models import CompanyUserProfile, User

from todos.models import TodoItem
from todos.services import MAX_STEPS_PER_TASK, user_today
from todos.tests import TodoTestCase

PASSWORD = 'Kx9#mQ2vLp8Z'


class BreakIntoStepsTests(TodoTestCase):
    def setUp(self):
        super().setUp()
        self.assignee = User.objects.create_user(
            email='assignee@example.com', username='assignee', password=PASSWORD,
        )
        CompanyUserProfile.objects.create(
            user=self.assignee, company=self.company_a, department=self.department_a,
            role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        self.task = self.create_task(assigned_to=self.assignee)

    def break_into_steps(self, actor, steps=None, task=None, **extra):
        body = {'steps': steps if steps is not None else ['Draft copy', 'Get sign-off', 'Publish']}
        body.update(extra)
        return self.client.post(
            f'/api/v1/todos/tasks/{(task or self.task).id}/steps/', json.dumps(body),
            content_type='application/json', **auth_header(actor),
        )

    def test_the_assignee_can_break_their_task_into_steps(self):
        response = self.break_into_steps(self.assignee)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(TodoItem.objects.filter(user=self.assignee, task=self.task).count(), 3)

    def test_the_steps_are_todos_not_tasks(self):
        """The point of the whole feature: nothing lands on the board."""
        self.break_into_steps(self.assignee)
        self.assertEqual(Task.objects.filter(project=self.project_a).count(), 1)

    def test_the_steps_keep_their_order(self):
        self.break_into_steps(self.assignee, steps=['First', 'Second', 'Third'])
        titles = list(
            TodoItem.objects.filter(user=self.assignee, task=self.task)
            .order_by('position').values_list('title', flat=True)
        )
        self.assertEqual(titles, ['First', 'Second', 'Third'])

    def test_they_snapshot_the_task_title(self):
        """Same rule as any other linked todo: the note stays readable after
        the task is renamed, archived, or the link is revoked."""
        self.break_into_steps(self.assignee)
        todo = TodoItem.objects.filter(user=self.assignee, task=self.task).first()
        self.assertEqual(todo.task_title_snapshot, self.task.title)

    def test_a_non_assignee_gets_the_same_answer_as_a_missing_task(self):
        """Collapsed into one response on purpose -- a non-assignee must not
        be able to probe which task ids exist."""
        response = self.break_into_steps(self.owner_a)
        self.assertEqual(response.status_code, 404, response.content)
        self.assertFalse(TodoItem.objects.filter(task=self.task).exists())

    def test_the_company_owner_is_no_exception(self):
        """No role widens access here, which is the one boundary this app
        has."""
        response = self.break_into_steps(self.owner_a)
        self.assertEqual(response.status_code, 404)

    def test_another_companys_owner_cannot_reach_the_task(self):
        response = self.break_into_steps(self.owner_b)
        self.assertEqual(response.status_code, 404, response.content)

    def test_the_steps_belong_to_the_assignee_and_nobody_else_can_read_them(self):
        self.break_into_steps(self.assignee)
        response = self.client.get('/api/v1/todos/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['data']['results']), 0)

    def test_blank_steps_are_dropped_and_an_all_blank_list_is_refused(self):
        response = self.break_into_steps(self.assignee, steps=['  ', 'Real step', ''])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(TodoItem.objects.filter(user=self.assignee, task=self.task).count(), 1)

    def test_an_empty_list_is_refused_by_the_schema(self):
        response = self.break_into_steps(self.assignee, steps=[])
        self.assertEqual(response.status_code, 422, response.content)

    def test_too_many_steps_is_refused(self):
        """A checklist that long is several tasks, and the answer to that is
        proposing them on the project."""
        response = self.break_into_steps(
            self.assignee, steps=[f'Step {i}' for i in range(MAX_STEPS_PER_TASK + 5)],
        )
        self.assertIn(response.status_code, (400, 422))
        self.assertFalse(TodoItem.objects.filter(task=self.task).exists())

    def test_steps_default_to_the_tasks_own_deadline(self):
        """Better than "today": the steps exist to finish this task, and today
        is only the right answer on the last day."""
        self.break_into_steps(self.assignee)
        todo = TodoItem.objects.filter(user=self.assignee, task=self.task).first()
        self.assertEqual(todo.due_date, timezone.localtime(self.task.deadline).date())

    def test_an_overdue_task_falls_back_to_today(self):
        """A to-do dated in the past would sort above everything and read as
        overdue the moment it was created."""
        self.task.deadline = timezone.now() - timedelta(days=3)
        self.task.save(update_fields=['deadline'])
        self.break_into_steps(self.assignee)
        todo = TodoItem.objects.filter(user=self.assignee, task=self.task).first()
        self.assertEqual(todo.due_date, user_today(self.assignee))

    def test_an_explicit_due_date_wins(self):
        chosen = (user_today(self.assignee) + timedelta(days=2)).isoformat()
        self.break_into_steps(self.assignee, due_date=chosen)
        todo = TodoItem.objects.filter(user=self.assignee, task=self.task).first()
        self.assertEqual(todo.due_date.isoformat(), chosen)

    def test_steps_append_after_existing_todos_on_the_same_day(self):
        """Positions are per (user, day); a new batch must not land on top of
        what is already there."""
        due = timezone.localtime(self.task.deadline).date()
        existing = TodoItem.objects.create(
            user=self.assignee, company=self.company_a, title='Already here',
            due_date=due, position=0,
        )
        self.break_into_steps(self.assignee)
        first_step = TodoItem.objects.filter(user=self.assignee, task=self.task).order_by('position').first()
        self.assertGreater(first_step.position, existing.position)

    def test_reassigning_the_task_hides_the_link_but_keeps_the_note(self):
        """The existing revoked-link rule, which these rows inherit for free:
        the owner keeps their private note and its snapshotted title, but
        loses the live link to work they are no longer part of."""
        self.break_into_steps(self.assignee)
        self.task.assigned_to = self.owner_a
        self.task.save(update_fields=['assigned_to'])

        response = self.client.get('/api/v1/todos/', **auth_header(self.assignee))
        results = response.json()['data']['results']
        self.assertEqual(len(results), 3)
        self.assertIsNone(results[0]['task_id'])
        self.assertEqual(results[0]['task_title'], 'Ship the landing page')
