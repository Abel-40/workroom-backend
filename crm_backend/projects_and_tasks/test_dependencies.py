"""Task dependencies: two kinds, one project, no loops.

`blocks` is hard and has exactly one observable consequence -- the successor
cannot leave To Do until the predecessor is Done -- checked at the transition
rather than cached, because a cached flag is wrong from the instant a
predecessor moves. `relates_to` is informational and gates nothing.

The interesting failure is the cycle. Two people adding A->B and B->A at the
same moment each read a graph without the other's edge and each conclude they
are safe; the result is two tasks permanently blocking each other with no route
out through the product. That is why the check-and-insert holds an advisory
lock, and why there is a concurrency test below rather than only a sequential
one.
"""

import json
import threading
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone
from users.models import CompanyUserProfile, User

from projects_and_tasks import services
from projects_and_tasks.models import Project, ProjectMembership, Task, TaskDependency
from projects_and_tasks.tasks import find_dependency_cycles

PASSWORD = 'Kx9#mQ2vLp8Z'


class DependencyWorldMixin:
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.project = Project.objects.create(
            title='Website Revamp', company=self.company_a, department=self.department_a,
            visibility=Project.VISIBILITY.COMPANY, start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )
        self.assignee = User.objects.create_user(
            email='assignee@example.com', username='assignee', password=PASSWORD,
        )
        CompanyUserProfile.objects.create(
            user=self.assignee, company=self.company_a, department=self.department_a,
            role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        self.first = self.make_task('Design the page')
        self.second = self.make_task('Build the page')

    def make_task(self, title, project=None, assigned_to=None):
        return Task.objects.create(
            project=project or self.project, title=title,
            assigned_to=assigned_to if assigned_to is not None else self.assignee,
            created_by=self.owner_a, deadline=timezone.now() + timedelta(days=30),
        )

    def add_edge(self, predecessor, successor, kind=TaskDependency.Kind.BLOCKS, actor=None):
        return async_to_sync(services.add_task_dependency)(
            actor or self.owner_a, predecessor, successor, kind,
        )

    def set_status(self, task, status, actor=None):
        return async_to_sync(services.update_task_status)(actor or self.assignee, task, status)


class BlocksGatesLeavingTodoTests(DependencyWorldMixin, TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.add_edge(self.first, self.second)

    def test_the_successor_cannot_leave_todo(self):
        blockers, error = self.set_status(self.second, Task.STATUS.IN_PROGRESS)
        self.assertEqual(error, 'blocked')
        self.second.refresh_from_db()
        self.assertEqual(self.second.status, Task.STATUS.TODO)

    def test_the_refusal_names_what_it_is_waiting_on(self):
        """A block with no explanation is indistinguishable from a bug."""
        blockers, error = self.set_status(self.second, Task.STATUS.IN_PROGRESS)
        self.assertEqual([task.id for task in blockers], [self.first.id])

    def test_the_predecessor_itself_is_free_to_move(self):
        _, error = self.set_status(self.first, Task.STATUS.IN_PROGRESS)
        self.assertIsNone(error)

    def test_finishing_the_predecessor_unblocks_it_immediately(self):
        """Live, not cached. Nothing recomputes a flag when the predecessor
        moves -- the question is asked at the transition."""
        self.first.status = Task.STATUS.DONE
        self.first.save(update_fields=['status'])

        updated, error = self.set_status(self.second, Task.STATUS.IN_PROGRESS)
        self.assertIsNone(error)
        self.assertEqual(updated.status, Task.STATUS.IN_PROGRESS)

    def test_an_archived_predecessor_blocks_nothing(self):
        """There is no route left to complete it, so leaving it in the way
        would strand the successor permanently."""
        self.first.is_deleted = True
        self.first.save(update_fields=['is_deleted'])

        _, error = self.set_status(self.second, Task.STATUS.IN_PROGRESS)
        self.assertIsNone(error)

    def test_a_task_already_past_todo_is_not_re_gated(self):
        """The rule is about leaving To Do. A task already in progress when a
        dependency is added is not dragged backwards."""
        self.second.status = Task.STATUS.IN_PROGRESS
        self.second.save(update_fields=['status'])

        _, error = self.set_status(self.second, Task.STATUS.TODO)
        self.assertIsNone(error)

    def test_relates_to_gates_nothing(self):
        third = self.make_task('Write the copy')
        fourth = self.make_task('Review the copy')
        self.add_edge(third, fourth, TaskDependency.Kind.RELATES_TO)

        _, error = self.set_status(fourth, Task.STATUS.IN_PROGRESS)
        self.assertIsNone(error)


class DependencyValidationTests(DependencyWorldMixin, TwoCompanyTestCase):
    def test_a_task_cannot_depend_on_itself(self):
        _, error = self.add_edge(self.first, self.first)
        self.assertEqual(error, 'self_dependency')

    def test_the_same_edge_cannot_be_added_twice(self):
        self.add_edge(self.first, self.second)
        _, error = self.add_edge(self.first, self.second)
        self.assertEqual(error, 'duplicate')

    def test_the_same_pair_may_hold_both_kinds(self):
        """They mean different things, and the uniqueness constraint is per
        kind for exactly that reason."""
        self.add_edge(self.first, self.second)
        _, error = self.add_edge(self.first, self.second, TaskDependency.Kind.RELATES_TO)
        self.assertIsNone(error)

    def test_cross_project_edges_are_refused(self):
        other_project = Project.objects.create(
            title='Mobile App', company=self.company_a, start_date=timezone.now(),
            deadline=timezone.now() + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )
        outside = self.make_task('Something else', project=other_project)
        _, error = self.add_edge(self.first, outside)
        self.assertEqual(error, 'different_projects')

    def test_a_direct_loop_is_refused(self):
        self.add_edge(self.first, self.second)
        _, error = self.add_edge(self.second, self.first)
        self.assertEqual(error, 'cycle')

    def test_a_longer_loop_is_refused(self):
        third = self.make_task('Ship the page')
        self.add_edge(self.first, self.second)
        self.add_edge(self.second, third)
        _, error = self.add_edge(third, self.first)
        self.assertEqual(error, 'cycle')

    def test_a_diamond_is_not_a_loop(self):
        """Two paths converging is ordinary planning, and a cycle check that
        confuses it for a loop would be useless."""
        left = self.make_task('Left branch')
        right = self.make_task('Right branch')
        end = self.make_task('Converge')
        self.add_edge(self.first, left)
        self.add_edge(self.first, right)
        self.add_edge(left, end)
        _, error = self.add_edge(right, end)
        self.assertIsNone(error)

    def test_relates_to_may_loop_freely(self):
        """It gates nothing, so a loop in it cannot strand anybody."""
        self.add_edge(self.first, self.second, TaskDependency.Kind.RELATES_TO)
        _, error = self.add_edge(self.second, self.first, TaskDependency.Kind.RELATES_TO)
        self.assertIsNone(error)


class DependencyAuthorityTests(DependencyWorldMixin, TwoCompanyTestCase):
    """Dependencies shape the work rather than do it, so they follow MANAGE."""

    def test_a_contributor_cannot_add_one(self):
        contributor = User.objects.create_user(
            email='contrib@example.com', username='contrib', password=PASSWORD,
        )
        CompanyUserProfile.objects.create(
            user=contributor, company=self.company_a, department=self.department_a,
            role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        ProjectMembership.objects.create(
            project=self.project, user=contributor,
            role=ProjectMembership.Role.CONTRIBUTOR, added_by=self.owner_a,
        )
        _, error = self.add_edge(self.first, self.second, actor=contributor)
        self.assertEqual(error, 'forbidden')

    def test_the_assignee_of_the_task_cannot_add_one_either(self):
        _, error = self.add_edge(self.first, self.second, actor=self.assignee)
        self.assertEqual(error, 'forbidden')

    def test_another_companys_owner_cannot_add_one(self):
        _, error = self.add_edge(self.first, self.second, actor=self.owner_b)
        self.assertEqual(error, 'forbidden')

    def test_a_manager_can(self):
        _, error = self.add_edge(self.first, self.second, actor=self.owner_a)
        self.assertIsNone(error)


class DependencyEndpointTests(DependencyWorldMixin, TwoCompanyTestCase):
    def post_edge(self, actor, task, other, direction='blocks', kind='blocks'):
        return self.client.post(
            f'/api/v1/tasks/{task.id}/dependencies/',
            json.dumps({'task_id': str(other.id), 'direction': direction, 'kind': kind}),
            content_type='application/json', **auth_header(actor),
        )

    def test_creating_through_the_api(self):
        response = self.post_edge(self.owner_a, self.first, self.second)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(
            TaskDependency.objects.filter(predecessor=self.first, successor=self.second).exists()
        )

    def test_blocked_by_direction_reverses_the_edge(self):
        """"this task is blocked by that one" and "this task blocks that one"
        are both natural for a UI to offer, and neither is more primary."""
        response = self.post_edge(self.owner_a, self.second, self.first, direction='blocked_by')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(
            TaskDependency.objects.filter(predecessor=self.first, successor=self.second).exists()
        )

    def test_listing_reports_direction_from_the_asking_tasks_point_of_view(self):
        self.add_edge(self.first, self.second)
        response = self.client.get(
            f'/api/v1/tasks/{self.second.id}/dependencies/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        edge = response.json()['data']['results'][0]
        self.assertEqual(edge['direction'], 'blocked_by')
        self.assertEqual(edge['task']['id'], str(self.first.id))

    def test_a_cycle_is_refused_with_an_explanation(self):
        self.add_edge(self.first, self.second)
        response = self.post_edge(self.owner_a, self.second, self.first)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('loop', response.json()['message'])

    def test_a_task_in_another_company_is_not_found(self):
        """Same answer as a task that does not exist -- otherwise this
        endpoint reports which ids are real to anyone holding any task."""
        other_project = Project.objects.create(
            title='Theirs', company=self.company_b, start_date=timezone.now(),
            deadline=timezone.now() + timedelta(days=90), created_by=self.owner_b,
        )
        theirs = Task.objects.create(
            project=other_project, title='Theirs', created_by=self.owner_b,
            deadline=timezone.now() + timedelta(days=30),
        )
        response = self.post_edge(self.owner_a, self.first, theirs)
        self.assertEqual(response.status_code, 404, response.content)

    def test_removing_through_the_api(self):
        dependency, _ = self.add_edge(self.first, self.second)
        response = self.client.delete(
            f'/api/v1/tasks/{self.first.id}/dependencies/{dependency.id}/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(TaskDependency.objects.filter(id=dependency.id).exists())

    def test_a_contributor_cannot_remove_one(self):
        dependency, _ = self.add_edge(self.first, self.second)
        response = self.client.delete(
            f'/api/v1/tasks/{self.first.id}/dependencies/{dependency.id}/', **auth_header(self.assignee),
        )
        self.assertEqual(response.status_code, 403, response.content)

    def test_a_blocked_transition_returns_the_blocker_titles(self):
        self.add_edge(self.first, self.second)
        response = self.client.patch(
            f'/api/v1/tasks/{self.second.id}/status/', json.dumps({'status': 'In Progress'}),
            content_type='application/json', **auth_header(self.assignee),
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()['errors']['blocked_by'], ['Design the page'])


class DependencyIntegrityCheckTests(DependencyWorldMixin, TwoCompanyTestCase):
    """The nightly backstop. It reports and never repairs -- which edge in a
    loop is the wrong one is a judgment about the work."""

    def test_a_clean_graph_reports_nothing(self):
        self.add_edge(self.first, self.second)
        self.assertEqual(find_dependency_cycles(), [])

    def test_a_cycle_inserted_behind_the_service_is_found(self):
        """Written straight to the model, bypassing the guarded path -- which
        is the only way a cycle can arrive, and exactly what the backstop is
        for."""
        TaskDependency.objects.create(predecessor=self.first, successor=self.second)
        TaskDependency.objects.create(predecessor=self.second, successor=self.first)

        cycles = find_dependency_cycles()
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {self.first.id, self.second.id})

    def test_a_longer_cycle_is_found(self):
        third = self.make_task('Ship the page')
        TaskDependency.objects.create(predecessor=self.first, successor=self.second)
        TaskDependency.objects.create(predecessor=self.second, successor=third)
        TaskDependency.objects.create(predecessor=third, successor=self.first)

        cycles = find_dependency_cycles()
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {self.first.id, self.second.id, third.id})

    def test_a_long_chain_does_not_exhaust_the_stack(self):
        """A sequential plan is a long chain, and the walk is iterative for
        this reason."""
        previous = self.first
        for index in range(300):
            current = self.make_task(f'Step {index}')
            TaskDependency.objects.create(predecessor=previous, successor=current)
            previous = current
        self.assertEqual(find_dependency_cycles(), [])

    def test_a_deleted_task_takes_its_cycle_out_of_scope(self):
        TaskDependency.objects.create(predecessor=self.first, successor=self.second)
        TaskDependency.objects.create(predecessor=self.second, successor=self.first)
        self.first.is_deleted = True
        self.first.save(update_fields=['is_deleted'])

        self.assertEqual(find_dependency_cycles(), [])


class ConcurrentCycleCreationTests(TransactionTestCase):
    """Two people closing a loop from both ends at the same moment.

    The case the advisory lock exists for. Each request reads a graph without
    the other's edge, each concludes it is safe, and without serialisation both
    insert -- leaving two tasks permanently blocking each other, from data that
    was valid when each was checked.

    TransactionTestCase because the threads need committed state to see each
    other; a TestCase wraps everything in one transaction nobody else can read.
    """

    def setUp(self):
        from company.models import Company, Sector

        sector = Sector.objects.create(name='Software')
        self.owner = User.objects.create_user(
            email='race-owner@example.com', username='race-owner', password=PASSWORD,
        )
        self.company = Company.objects.create(name='Race Co', owner=self.owner, sector=sector)
        CompanyUserProfile.objects.create(
            user=self.owner, company=self.company, role=CompanyUserProfile.Role.Owner,
        )
        now = timezone.now()
        self.project = Project.objects.create(
            title='Race', company=self.company, start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner, current_owner=self.owner,
        )
        self.a = Task.objects.create(
            project=self.project, title='A', created_by=self.owner, deadline=now + timedelta(days=30),
        )
        self.b = Task.objects.create(
            project=self.project, title='B', created_by=self.owner, deadline=now + timedelta(days=30),
        )

    def test_only_one_of_two_opposing_edges_survives(self):
        start = threading.Barrier(2)
        results = {}

        def attempt(name, predecessor, successor):
            try:
                start.wait(timeout=10)
                results[name] = services._insert_dependency_checked(
                    self.owner, predecessor, successor, TaskDependency.Kind.BLOCKS,
                )
            finally:
                # Each thread gets its own connection; leaving it open would
                # hold the test database busy at teardown.
                connections.close_all()

        threads = [
            threading.Thread(target=attempt, args=('forward', self.a, self.b)),
            threading.Thread(target=attempt, args=('backward', self.b, self.a)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(results), 2, 'both attempts should have finished')
        errors = [error for _, error in results.values()]
        self.assertEqual(sorted(e or 'ok' for e in errors), ['cycle', 'ok'])
        self.assertEqual(TaskDependency.objects.count(), 1)
        self.assertEqual(find_dependency_cycles(), [])
