"""To-do timezone rules, AI eligibility, and supersede semantics.

Three things §9 asks for, each of which was previously wrong in a way that
made the feature quietly worse rather than visibly broken:

- a manual due date could be in the past, and "today" was not the owner's
- the AI drew on any open assigned task, including work that cannot start
- asking again produced a second overlapping list rather than replacing the
  first

The rule underneath all of the supersede behaviour: **no AI operation ever
deletes something a person typed.**
"""

import json
from datetime import timedelta

from ai_agent.models import AITodoGeneration
from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from django.utils import timezone
from projects_and_tasks.models import Project, Task, TaskDependency

from todos import services
from todos.models import TodoItem
from todos.services import user_today

PASSWORD = 'Kx9#mQ2vLp8Z'


class GenerationWorldMixin:
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.project = Project.objects.create(
            title='Website Revamp', company=self.company_a, department=self.department_a,
            visibility=Project.VISIBILITY.COMPANY, start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
            status=Project.STATUS.ACTIVE,
        )
        self.today = user_today(self.member_a)

    UNSET = object()

    def make_task(self, title='Design the page', *, assigned_to=UNSET, status=Task.STATUS.TODO,
                  project=None, days=0, is_deleted=False):
        return Task.objects.create(
            project=project or self.project, title=title,
            assigned_to=self.member_a if assigned_to is self.UNSET else assigned_to,
            created_by=self.owner_a, status=status, is_deleted=is_deleted,
            deadline=timezone.now() + timedelta(days=days),
        )

    def eligible(self, user=None, window_end=None):
        """Default window is today, matching 'today' mode -- so these read as
        "what would a daily checklist draw on"."""
        user = user or self.member_a
        return list(services.eligible_tasks_for_generation(user, self.company_a, window_end=window_end))


class ManualDueDateTests(GenerationWorldMixin, TwoCompanyTestCase):
    """§9: a manual to-do is dated today or later, in the owner's timezone."""

    def create(self, due_date, user=None):
        return self.client.post(
            '/api/v1/todos/', json.dumps({'title': 'Draft the brief', 'due_date': due_date.isoformat()}),
            content_type='application/json', **auth_header(user or self.member_a),
        )

    def test_today_is_accepted(self):
        self.assertEqual(self.create(self.today).status_code, 201)

    def test_a_future_day_is_accepted(self):
        self.assertEqual(self.create(self.today + timedelta(days=5)).status_code, 201)

    def test_yesterday_is_refused(self):
        """Previously allowed on the reasoning that backfilling is legitimate.
        An overdue to-do is a state a to-do arrives at, not one it should be
        created in -- creating one already late is almost always a typo."""
        response = self.create(self.today - timedelta(days=1))
        self.assertEqual(response.status_code, 400, response.content)

    def test_the_refusal_says_what_to_do(self):
        response = self.create(self.today - timedelta(days=1))
        self.assertIn('today or a later day', response.json()['message'])

    def test_today_is_the_owners_today_not_the_servers(self):
        """The whole point of computing it per user: for somebody far enough
        east, the server's date is yesterday for part of every day."""
        self.member_a.timezone = 'Pacific/Kiritimati'  # UTC+14
        self.member_a.save(update_fields=['timezone'])
        their_today = user_today(self.member_a)
        self.assertEqual(self.create(their_today, self.member_a).status_code, 201)

    def test_moving_an_existing_todo_into_the_past_is_refused(self):
        created = self.create(self.today).json()['data']['todo']
        response = self.client.patch(
            f"/api/v1/todos/{created['id']}/",
            json.dumps({'due_date': (self.today - timedelta(days=2)).isoformat()}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_an_ai_generated_todo_may_be_dated_in_the_past(self):
        """Because §9 requires overdue work to be included and ranked first,
        the AI path has to be able to date a to-do to an overdue task's day."""
        todo, error = async_to_sync(services.create_todo)(
            self.member_a, self.company_a, title='Catch up',
            due_date=self.today - timedelta(days=2),
            source=TodoItem.SOURCE.AI_GENERATED,
        )
        self.assertIsNone(error)
        self.assertIsNotNone(todo)


class EligibilityTests(GenerationWorldMixin, TwoCompanyTestCase):
    """What the AI may draw on. Every clause of §9's list, one test each."""

    def test_an_assigned_open_task_qualifies(self):
        task = self.make_task()
        self.assertIn(task, self.eligible())

    def test_someone_elses_task_does_not(self):
        self.make_task(assigned_to=self.owner_a)
        self.assertEqual(self.eligible(), [])

    def test_an_unassigned_task_does_not(self):
        self.make_task(assigned_to=None)
        self.assertEqual(self.eligible(), [])

    def test_a_done_task_does_not(self):
        self.make_task(status=Task.STATUS.DONE)
        self.assertEqual(self.eligible(), [])

    def test_an_in_review_task_does_not(self):
        """It is waiting on somebody else's decision, so there is nothing for
        the assignee to plan."""
        self.make_task(status=Task.STATUS.IN_REVIEW)
        self.assertEqual(self.eligible(), [])

    def test_an_in_progress_task_does(self):
        task = self.make_task(status=Task.STATUS.IN_PROGRESS)
        self.assertIn(task, self.eligible())

    def test_an_archived_task_does_not(self):
        self.make_task(is_deleted=True)
        self.assertEqual(self.eligible(), [])

    def test_a_task_on_an_archived_project_does_not(self):
        self.project.is_deleted = True
        self.project.save(update_fields=['is_deleted'])
        self.make_task()
        self.assertEqual(self.eligible(), [])

    def test_a_task_on_an_inactive_project_does_not(self):
        self.project.status = Project.STATUS.INACTIVE
        self.project.save(update_fields=['status'])
        self.make_task()
        self.assertEqual(self.eligible(), [])

    def test_a_blocked_task_does_not(self):
        """Planning steps for work that cannot start is worse than planning
        nothing -- it looks like a plan."""
        blocker = self.make_task('Design', assigned_to=self.owner_a)
        blocked = self.make_task('Build')
        TaskDependency.objects.create(predecessor=blocker, successor=blocked)
        self.assertNotIn(blocked, self.eligible())

    def test_it_qualifies_once_the_blocker_is_done(self):
        blocker = self.make_task('Design', assigned_to=self.owner_a)
        blocked = self.make_task('Build')
        TaskDependency.objects.create(predecessor=blocker, successor=blocked)
        blocker.status = Task.STATUS.DONE
        blocker.save(update_fields=['status'])
        self.assertIn(blocked, self.eligible())

    def test_a_relates_to_edge_does_not_block(self):
        other = self.make_task('Copy', assigned_to=self.owner_a)
        task = self.make_task('Build')
        TaskDependency.objects.create(
            predecessor=other, successor=task, kind=TaskDependency.Kind.RELATES_TO,
        )
        self.assertIn(task, self.eligible())

    def test_a_task_due_beyond_the_window_does_not(self):
        """'today' mode is a daily focus list, not a backlog: §9 excludes
        out-of-window-future work explicitly."""
        self.make_task(days=60)
        self.assertEqual(self.eligible(), [])

    def test_it_qualifies_once_the_window_reaches_it(self):
        task = self.make_task(days=5)
        self.assertEqual(self.eligible(), [])
        self.assertIn(task, self.eligible(window_end=self.today + timedelta(days=7)))

    def test_an_overdue_task_is_included(self):
        """The most urgent thing the person owns. A "what am I doing today"
        list that silently omits everything already late reads as
        reassurance."""
        overdue = self.make_task('Late', days=-3)
        self.assertIn(overdue, self.eligible())

    def test_overdue_tasks_come_first(self):
        """Both are in the window here -- 'Soon' is due today -- so this is
        about ordering, not inclusion."""
        soon = self.make_task('Soon', days=0)
        overdue = self.make_task('Late', days=-3)
        eligible = self.eligible()
        self.assertEqual(eligible[0].id, overdue.id)
        self.assertIn(soon, eligible)

    def test_another_companys_task_does_not(self):
        other_project = Project.objects.create(
            title='Theirs', company=self.company_b, start_date=timezone.now(),
            deadline=timezone.now() + timedelta(days=90), created_by=self.owner_b,
        )
        self.make_task(project=other_project)
        self.assertEqual(self.eligible(), [])


class SupersedeTests(GenerationWorldMixin, TwoCompanyTestCase):
    """Asking again replaces the previous plan. The rule that governs it: no
    AI operation deletes something a person typed."""

    def setUp(self):
        super().setUp()
        self.generation = AITodoGeneration.objects.create(
            user=self.member_a, company=self.company_a, mode='today',
            window_start=self.today, window_end=self.today,
            status=AITodoGeneration.STATUS.COMPLETED,
        )

    def make_todo(self, title, *, done=False, source=TodoItem.SOURCE.AI_GENERATED, generation=None):
        return TodoItem.objects.create(
            user=self.member_a, company=self.company_a, title=title, due_date=self.today,
            source=source,
            ai_generation=generation if generation is not None else (
                self.generation if source == TodoItem.SOURCE.AI_GENERATED else None
            ),
            is_done=done, completed_at=timezone.now() if done else None,
        )

    def supersede(self):
        return async_to_sync(services.supersede_generation)(self.member_a, self.generation)

    def test_incomplete_ai_items_are_removed(self):
        todo = self.make_todo('Stale step')
        self.supersede()
        todo.refresh_from_db()
        self.assertTrue(todo.is_deleted)

    def test_completed_items_are_kept(self):
        """The owner did that work. Deleting it would destroy their record,
        not tidy ours."""
        todo = self.make_todo('Finished step', done=True)
        self.supersede()
        todo.refresh_from_db()
        self.assertFalse(todo.is_deleted)

    def test_completed_items_are_detached_from_the_old_generation(self):
        """So a later dismissal of the superseded generation cannot reach back
        and take them."""
        todo = self.make_todo('Finished step', done=True)
        self.supersede()
        todo.refresh_from_db()
        self.assertIsNone(todo.ai_generation_id)

    def test_manual_todos_are_never_touched(self):
        manual = self.make_todo('My own note', source=TodoItem.SOURCE.MANUAL)
        self.supersede()
        manual.refresh_from_db()
        self.assertFalse(manual.is_deleted)

    def test_a_manual_todo_attached_to_the_generation_is_still_safe(self):
        """Belt and braces: the filter names the source as well as the
        generation, so even a mis-attached manual row survives."""
        manual = TodoItem.objects.create(
            user=self.member_a, company=self.company_a, title='Typed by hand',
            due_date=self.today, source=TodoItem.SOURCE.MANUAL, ai_generation=self.generation,
        )
        self.supersede()
        manual.refresh_from_db()
        self.assertFalse(manual.is_deleted)

    def test_another_users_todos_are_untouched(self):
        theirs = TodoItem.objects.create(
            user=self.owner_a, company=self.company_a, title='Not mine',
            due_date=self.today, source=TodoItem.SOURCE.AI_GENERATED, ai_generation=self.generation,
        )
        self.supersede()
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_deleted)

    def test_the_previous_plan_for_today_is_found(self):
        found = async_to_sync(services.find_superseded_generation)(self.member_a, mode='today')
        self.assertEqual(found.id, self.generation.id)

    def test_a_plan_for_another_day_is_not_superseded(self):
        self.generation.window_start = self.today - timedelta(days=1)
        self.generation.save(update_fields=['window_start'])
        found = async_to_sync(services.find_superseded_generation)(self.member_a, mode='today')
        self.assertIsNone(found)

    def test_task_mode_supersedes_per_task(self):
        task = self.make_task()
        other = self.make_task('Another')
        generation = AITodoGeneration.objects.create(
            user=self.member_a, company=self.company_a, mode='task', task=task,
            window_start=self.today, window_end=self.today,
            status=AITodoGeneration.STATUS.COMPLETED,
        )
        found = async_to_sync(services.find_superseded_generation)(self.member_a, mode='task', task=task)
        self.assertEqual(found.id, generation.id)
        self.assertIsNone(
            async_to_sync(services.find_superseded_generation)(self.member_a, mode='task', task=other)
        )


class GenerationQuotaTests(GenerationWorldMixin, TwoCompanyTestCase):
    """§9: ten per user per day, counted in the user's own timezone."""

    def make_generations(self, count, *, user=None, when=None):
        user = user or self.member_a
        for index in range(count):
            generation = AITodoGeneration.objects.create(
                user=user, company=self.company_a, mode='today',
                window_start=self.today, window_end=self.today,
                status=AITodoGeneration.STATUS.COMPLETED,
            )
            if when is not None:
                AITodoGeneration.objects.filter(id=generation.id).update(requested_at=when)

    def test_under_the_cap_is_allowed(self):
        self.make_generations(services.MAX_GENERATIONS_PER_DAY - 1)
        self.assertIsNone(async_to_sync(services.check_generation_quota)(self.member_a))

    def test_at_the_cap_is_refused(self):
        self.make_generations(services.MAX_GENERATIONS_PER_DAY)
        self.assertEqual(
            async_to_sync(services.check_generation_quota)(self.member_a), 'daily_limit_reached',
        )

    def test_yesterdays_generations_do_not_count(self):
        self.make_generations(
            services.MAX_GENERATIONS_PER_DAY, when=timezone.now() - timedelta(days=1),
        )
        self.assertIsNone(async_to_sync(services.check_generation_quota)(self.member_a))

    def test_the_cap_is_per_user(self):
        self.make_generations(services.MAX_GENERATIONS_PER_DAY, user=self.owner_a)
        self.assertIsNone(async_to_sync(services.check_generation_quota)(self.member_a))

    def test_the_endpoint_refuses_with_429(self):
        self.make_task()
        self.make_generations(services.MAX_GENERATIONS_PER_DAY)
        response = self.client.post(
            '/api/v1/todos/generate/', json.dumps({'mode': 'today'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 429, response.content)

    def test_the_refusal_says_when_it_resets(self):
        self.make_task()
        self.make_generations(services.MAX_GENERATIONS_PER_DAY)
        response = self.client.post(
            '/api/v1/todos/generate/', json.dumps({'mode': 'today'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertIn('own timezone', response.json()['message'])
