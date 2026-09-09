"""Workload arithmetic, and the policy built on top of it.

The rules that matter here are about honesty as much as authorization: the
hours figure says when it is incomplete, the utilisation limit refuses to fire
on a number it knows is short, and warn mode is a message rather than a
refusal.
"""

import json
from datetime import timedelta
from decimal import Decimal

from api.tests import TwoCompanyTestCase, auth_header
from audit.models import AuditAction, AuditEvent
from django.test import TransactionTestCase
from django.utils import timezone
from projects_and_tasks.models import ApprovalRequest, Project, Task
from users.models import CompanyUserProfile, User

from workforce.models import AssignmentPolicy
from workforce.workload import (
    HORIZON_DAYS,
    member_workload_sync,
    prorated_capacity_hours,
)

PASSWORD = 'Kx9#mQ2vLp8Z'


class WorkloadFixture(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.manager_a = User.objects.create_user(
            email='cm-wl@example.com', username='cm-wl', password=PASSWORD,
        )
        CompanyUserProfile.objects.create(
            user=self.manager_a, company=self.company_a,
            role=CompanyUserProfile.Role.COMPANY_MANAGER,
        )
        self.project = Project.objects.create(
            title='Load Test', company=self.company_a, created_by=self.owner_a,
            current_owner=self.owner_a, visibility='company',
            deadline=timezone.now() + timedelta(days=365), status=Project.STATUS.ACTIVE,
        )

    def profile_of(self, user):
        return CompanyUserProfile.objects.get(user=user, company=self.company_a)

    def make_task(self, *, assignee=None, hours=None, days_out=3, status=Task.STATUS.TODO,
                  project=None, deleted=False):
        return Task.objects.create(
            project=project or self.project, assigned_to=assignee, title='Work',
            deadline=timezone.now() + timedelta(days=days_out), status=status,
            estimated_time=timedelta(hours=hours) if hours is not None else None,
            created_by=self.owner_a, is_deleted=deleted,
        )

    def set_policy(self, **fields):
        policy, _ = AssignmentPolicy.objects.get_or_create(company=self.company_a)
        for key, value in fields.items():
            setattr(policy, key, value)
        policy.save()
        return policy

    def set_capacity(self, user, hours, availability=None):
        CompanyUserProfile.objects.filter(user=user, company=self.company_a).update(
            weekly_capacity_hours=hours, availability=availability or {},
        )


class CapacityProrationTests(WorkloadFixture):
    def test_unstated_capacity_is_not_zero(self):
        """Unknown and none are different answers. Treating unstated as zero
        would make every member who never opened the setting permanently over
        any utilisation limit."""
        self.assertIsNone(prorated_capacity_hours(self.profile_of(self.member_a)))

    def test_a_default_week_prorates_over_the_horizon(self):
        self.set_capacity(self.member_a, 40)
        # 14 days spans exactly two Mon-Fri weeks, whichever day it starts on.
        capacity = prorated_capacity_hours(self.profile_of(self.member_a))
        self.assertEqual(capacity, Decimal('80.00'))

    def test_a_shorter_week_scales_the_same_weekly_total(self):
        """Somebody contracted for 24 hours across three days works the same
        24 hours a week -- proration divides by the days they work, not by
        five."""
        self.set_capacity(self.member_a, 24, {'working_days': ['mon', 'tue', 'wed']})
        self.assertEqual(prorated_capacity_hours(self.profile_of(self.member_a)), Decimal('48.00'))

    def test_time_off_reduces_capacity(self):
        today = timezone.localdate()
        self.set_capacity(self.member_a, 40, {
            'time_off': [{
                'start': today.isoformat(),
                'end': (today + timedelta(days=HORIZON_DAYS)).isoformat(),
            }],
        })
        self.assertEqual(prorated_capacity_hours(self.profile_of(self.member_a)), Decimal('0.00'))

    def test_time_off_is_inclusive_at_both_ends(self):
        """"Off from the 1st to the 5th" is five days, not four."""
        # A seven-day week so the assertion is about the time-off arithmetic
        # rather than about which weekdays the horizon happens to start on.
        self.set_capacity(self.member_a, 70, {
            'working_days': ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
        })
        full = prorated_capacity_hours(self.profile_of(self.member_a))
        today = timezone.localdate()
        self.set_capacity(self.member_a, 70, {
            'working_days': ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
            'time_off': [{'start': today.isoformat(), 'end': (today + timedelta(days=4)).isoformat()}],
        })
        reduced = prorated_capacity_hours(self.profile_of(self.member_a))
        self.assertEqual(full - reduced, Decimal('50.00'))  # 5 days x 10h


class WorkloadFigureTests(WorkloadFixture):
    def test_an_empty_workload(self):
        snapshot = member_workload_sync(self.company_a, self.member_a)
        self.assertEqual(snapshot.active_task_count, 0)
        self.assertEqual(snapshot.committed_hours, Decimal('0.00'))
        self.assertTrue(snapshot.hours_complete)
        self.assertIsNone(snapshot.utilisation_pct)

    def test_counts_open_work_and_ignores_done(self):
        self.make_task(assignee=self.member_a, hours=4)
        self.make_task(assignee=self.member_a, hours=2, status=Task.STATUS.IN_PROGRESS)
        self.make_task(assignee=self.member_a, hours=1, status=Task.STATUS.IN_REVIEW)
        self.make_task(assignee=self.member_a, hours=8, status=Task.STATUS.DONE)
        snapshot = member_workload_sync(self.company_a, self.member_a)
        self.assertEqual(snapshot.active_task_count, 3)
        self.assertEqual(snapshot.committed_hours, Decimal('7.00'))

    def test_ignores_deleted_tasks_and_inactive_projects(self):
        self.make_task(assignee=self.member_a, hours=4, deleted=True)
        archived = Project.objects.create(
            title='Archived', company=self.company_a, created_by=self.owner_a,
            deadline=timezone.now() + timedelta(days=365), status=Project.STATUS.INACTIVE,
        )
        self.make_task(assignee=self.member_a, hours=4, project=archived)
        self.assertEqual(member_workload_sync(self.company_a, self.member_a).active_task_count, 0)

    def test_hours_count_only_the_horizon_but_the_task_count_does_not(self):
        """A task due in three months is still something you are on the hook
        for; it is not something the next fortnight has to absorb."""
        self.make_task(assignee=self.member_a, hours=5, days_out=2)
        self.make_task(assignee=self.member_a, hours=100, days_out=90)
        snapshot = member_workload_sync(self.company_a, self.member_a)
        self.assertEqual(snapshot.active_task_count, 2)
        self.assertEqual(snapshot.committed_hours, Decimal('5.00'))

    def test_overdue_work_still_counts_against_the_horizon(self):
        self.make_task(assignee=self.member_a, hours=6, days_out=-3)
        self.assertEqual(member_workload_sync(self.company_a, self.member_a).committed_hours, Decimal('6.00'))

    def test_a_missing_estimate_makes_the_hours_figure_incomplete(self):
        """The number would otherwise be quietly short, which is worse than
        no number: it makes an overloaded person look free."""
        self.make_task(assignee=self.member_a, hours=4)
        self.make_task(assignee=self.member_a, hours=None)
        snapshot = member_workload_sync(self.company_a, self.member_a)
        self.assertEqual(snapshot.committed_hours, Decimal('4.00'))
        self.assertFalse(snapshot.hours_complete)
        self.assertEqual(snapshot.tasks_missing_estimate, 1)

    def test_utilisation_needs_a_stated_capacity(self):
        self.make_task(assignee=self.member_a, hours=40)
        self.assertIsNone(member_workload_sync(self.company_a, self.member_a).utilisation_pct)
        self.set_capacity(self.member_a, 40)
        self.assertEqual(member_workload_sync(self.company_a, self.member_a).utilisation_pct, Decimal('50.0'))

    def test_workload_is_scoped_to_one_company(self):
        other_project = Project.objects.create(
            title='Theirs', company=self.company_b, created_by=self.owner_b,
            deadline=timezone.now() + timedelta(days=365), status=Project.STATUS.ACTIVE,
        )
        CompanyUserProfile.objects.create(
            user=self.member_a, company=self.company_b, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        self.make_task(assignee=self.member_a, hours=4, project=other_project)
        self.assertEqual(member_workload_sync(self.company_a, self.member_a).active_task_count, 0)
        self.assertEqual(member_workload_sync(self.company_b, self.member_a).active_task_count, 1)


class AssignmentPolicyTests(WorkloadFixture):
    def assign(self, actor, task, assignee, **body):
        payload = {'assigned_to_id': str(assignee.id) if assignee else None}
        payload.update(body)
        return self.client.post(
            f'/api/v1/tasks/{task.id}/assign/', json.dumps(payload),
            content_type='application/json', **auth_header(actor),
        )

    def test_no_policy_means_no_limit(self):
        for _ in range(5):
            self.make_task(assignee=self.member_a)
        task = self.make_task()
        response = self.assign(self.owner_a, task, self.member_a)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()['data']['workload'])

    def test_a_disabled_policy_means_no_limit(self):
        self.set_policy(enabled=False, max_active_tasks=1)
        self.make_task(assignee=self.member_a)
        task = self.make_task()
        self.assertEqual(self.assign(self.owner_a, task, self.member_a).status_code, 200)

    def test_warn_mode_lets_it_through_and_returns_the_number(self):
        self.set_policy(enabled=True, max_active_tasks=1, enforcement='warn')
        self.make_task(assignee=self.member_a)
        task = self.make_task()
        response = self.assign(self.owner_a, task, self.member_a)
        self.assertEqual(response.status_code, 200)
        notice = response.json()['data']['workload']
        self.assertEqual(notice['breached'], ['max_active_tasks'])
        self.assertEqual(notice['enforcement'], 'warn')
        self.assertEqual(notice['workload']['active_task_count'], 1)
        self.assertEqual(Task.objects.get(id=task.id).assigned_to_id, self.member_a.id)

    def test_the_limit_is_a_ceiling_on_the_result(self):
        """max_active_tasks=2 means two. The third assignment is the one that
        crosses it, not the second."""
        self.set_policy(enabled=True, max_active_tasks=2, enforcement='warn')
        self.make_task(assignee=self.member_a)
        second = self.make_task()
        self.assertIsNone(self.assign(self.owner_a, second, self.member_a).json()['data']['workload'])
        third = self.make_task()
        self.assertIsNotNone(self.assign(self.owner_a, third, self.member_a).json()['data']['workload'])

    def test_block_mode_refuses_and_names_the_route_forward(self):
        self.set_policy(enabled=True, max_active_tasks=1, enforcement='block', override_roles=['Owner'])
        self.make_task(assignee=self.member_a)
        task = self.make_task()
        response = self.assign(self.manager_a, task, self.member_a)
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertTrue(body['data']['workload']['needs_approval'])
        self.assertIn('workload-override', body['message'])
        self.assertIsNone(Task.objects.get(id=task.id).assigned_to_id)

    def test_an_authorized_role_may_override_with_a_reason(self):
        self.set_policy(enabled=True, max_active_tasks=1, enforcement='block', override_roles=['Owner'])
        self.make_task(assignee=self.member_a)
        task = self.make_task()
        self.assertEqual(self.assign(self.owner_a, task, self.member_a).status_code, 403)
        response = self.assign(self.owner_a, task, self.member_a, override_reason='Critical release')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Task.objects.get(id=task.id).assigned_to_id, self.member_a.id)

    def test_an_override_is_audited_with_its_reason(self):
        self.set_policy(enabled=True, max_active_tasks=1, enforcement='block', override_roles=['Owner'])
        self.make_task(assignee=self.member_a)
        task = self.make_task()
        before = AuditEvent.objects.filter(action=AuditAction.TASK_ASSIGNED_OVER_LIMIT).count()
        self.assign(self.owner_a, task, self.member_a, override_reason='Critical release')
        rows = AuditEvent.objects.filter(action=AuditAction.TASK_ASSIGNED_OVER_LIMIT)
        self.assertEqual(rows.count(), before + 1)
        self.assertEqual(rows.order_by('-created_at').first().reason, 'Critical release')

    def test_a_warned_assignment_is_audited_too(self):
        """Nothing stopped it, so the audit row is the only record that
        somebody was told and went ahead anyway."""
        self.set_policy(enabled=True, max_active_tasks=1, enforcement='warn')
        self.make_task(assignee=self.member_a)
        task = self.make_task()
        before = AuditEvent.objects.filter(action=AuditAction.TASK_ASSIGNED_OVER_LIMIT).count()
        self.assign(self.owner_a, task, self.member_a)
        self.assertEqual(
            AuditEvent.objects.filter(action=AuditAction.TASK_ASSIGNED_OVER_LIMIT).count(), before + 1,
        )

    def test_utilisation_does_not_fire_on_an_incomplete_figure(self):
        """Refusing on a number known to be short would be refusing on a
        number known to be wrong."""
        self.set_capacity(self.member_a, 10)
        self.set_policy(enabled=True, max_utilisation_pct=50, enforcement='block', override_roles=['Owner'])
        self.make_task(assignee=self.member_a, hours=20)     # 20h against 20h capacity
        self.make_task(assignee=self.member_a, hours=None)   # and one unknown
        task = self.make_task()
        self.assertEqual(self.assign(self.owner_a, task, self.member_a).status_code, 200)

    def test_utilisation_fires_when_the_figure_is_complete(self):
        self.set_capacity(self.member_a, 10)   # 20h across the horizon
        self.set_policy(enabled=True, max_utilisation_pct=50, enforcement='block', override_roles=['Owner'])
        self.make_task(assignee=self.member_a, hours=15)     # 75%
        task = self.make_task()
        response = self.assign(self.owner_a, task, self.member_a)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['data']['workload']['breached'], ['max_utilisation_pct'])

    def test_utilisation_cannot_fire_without_a_stated_capacity(self):
        self.set_policy(enabled=True, max_utilisation_pct=1, enforcement='block', override_roles=['Owner'])
        self.make_task(assignee=self.member_a, hours=100)
        task = self.make_task()
        self.assertEqual(self.assign(self.owner_a, task, self.member_a).status_code, 200)

    def test_unassigning_is_never_blocked(self):
        self.set_policy(enabled=True, max_active_tasks=1, enforcement='block', override_roles=[])
        task = self.make_task(assignee=self.member_a)
        self.assertEqual(self.assign(self.owner_a, task, None).status_code, 200)

    def test_reassigning_to_the_same_person_is_never_blocked(self):
        """A no-op cannot put anybody over a limit, and refusing it would
        strand a task whose holder is already at cap."""
        self.set_policy(enabled=True, max_active_tasks=1, enforcement='block', override_roles=[])
        task = self.make_task(assignee=self.member_a)
        self.make_task(assignee=self.member_a)
        self.assertEqual(self.assign(self.owner_a, task, self.member_a).status_code, 200)

    def test_creating_a_task_already_assigned_is_checked_too(self):
        """A limit you can step around by setting the assignee at creation is
        not a limit."""
        self.set_policy(enabled=True, max_active_tasks=1, enforcement='block', override_roles=[])
        self.make_task(assignee=self.member_a)
        response = self.client.post(
            f'/api/v1/projects/{self.project.id}/tasks/',
            json.dumps({
                'title': 'Sneaky', 'assigned_to_id': str(self.member_a.id),
                'deadline': (timezone.now() + timedelta(days=5)).isoformat(),
            }),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Task.objects.filter(title='Sneaky').exists())

    def test_the_policy_is_scoped_to_one_company(self):
        self.set_policy(enabled=True, max_active_tasks=1, enforcement='block', override_roles=[])
        other_project = Project.objects.create(
            title='Theirs', company=self.company_b, created_by=self.owner_b,
            current_owner=self.owner_b, deadline=timezone.now() + timedelta(days=365),
            status=Project.STATUS.ACTIVE, visibility='company',
        )
        for _ in range(3):
            Task.objects.create(
                project=other_project, assigned_to=self.owner_b, title='Work',
                deadline=timezone.now() + timedelta(days=3), created_by=self.owner_b,
            )
        task = Task.objects.create(
            project=other_project, title='More', deadline=timezone.now() + timedelta(days=3),
            created_by=self.owner_b,
        )
        self.assertEqual(self.assign(self.owner_b, task, self.owner_b).status_code, 200)


class AssignmentPolicySettingsTests(WorkloadFixture):
    def put_policy(self, actor, **body):
        return self.client.put(
            '/api/v1/workforce/assignment-policy/', json.dumps(body),
            content_type='application/json', **auth_header(actor),
        )

    def test_absent_policy_reads_as_off(self):
        response = self.client.get('/api/v1/workforce/assignment-policy/', **auth_header(self.member_a))
        self.assertEqual(response.status_code, 200)
        policy = response.json()['data']['policy']
        self.assertFalse(policy['enabled'])
        self.assertEqual(policy['effective_enforcement'], 'off')

    def test_owner_and_manager_may_set_it(self):
        for actor in (self.owner_a, self.manager_a):
            response = self.put_policy(actor, enabled=True, max_active_tasks=5)
            self.assertEqual(response.status_code, 200, response.content)

    def test_a_department_member_may_not(self):
        self.assertEqual(self.put_policy(self.member_a, enabled=True).status_code, 403)
        self.assertFalse(AssignmentPolicy.objects.filter(company=self.company_a, enabled=True).exists())

    def test_setting_it_twice_updates_one_row(self):
        self.put_policy(self.owner_a, enabled=True, max_active_tasks=5)
        self.put_policy(self.owner_a, max_active_tasks=8)
        self.assertEqual(AssignmentPolicy.objects.filter(company=self.company_a).count(), 1)
        self.assertEqual(AssignmentPolicy.objects.get(company=self.company_a).max_active_tasks, 8)

    def test_cross_tenant_policies_stay_separate(self):
        self.put_policy(self.owner_a, enabled=True, max_active_tasks=5)
        response = self.client.get('/api/v1/workforce/assignment-policy/', **auth_header(self.owner_b))
        self.assertFalse(response.json()['data']['policy']['enabled'])

    def test_an_unknown_role_is_refused_by_the_schema(self):
        self.assertEqual(self.put_policy(self.owner_a, override_roles=['Wizard']).status_code, 422)


class WorkloadOverrideRequestTests(WorkloadFixture):
    def setUp(self):
        super().setUp()
        self.set_policy(enabled=True, max_active_tasks=1, enforcement='block', override_roles=['Owner'])
        self.make_task(assignee=self.member_a)
        self.task = self.make_task()

    def request_override(self, actor, task=None, **body):
        payload = {'assigned_to_id': str(self.member_a.id), 'reason': 'Nobody else knows this system'}
        payload.update(body)
        return self.client.post(
            f'/api/v1/tasks/{(task or self.task).id}/workload-override/', json.dumps(payload),
            content_type='application/json', **auth_header(actor),
        )

    def decide(self, actor, request_id, action='approve', comment=''):
        return self.client.post(
            f'/api/v1/workload-overrides/{request_id}/{action}/', json.dumps({'comment': comment}),
            content_type='application/json', **auth_header(actor),
        )

    def test_an_unauthorized_manager_can_ask(self):
        response = self.request_override(self.manager_a)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            ApprovalRequest.objects.filter(kind='workload_override', status='pending').count(), 1,
        )

    def test_a_second_open_ask_on_the_same_task_is_refused(self):
        """One open ask per task -- a second is not a second opinion, it is a
        race between two answers."""
        self.request_override(self.manager_a)
        self.assertEqual(self.request_override(self.manager_a).status_code, 400)

    def test_asking_when_nothing_is_over_the_line_is_refused(self):
        free = self.make_task()
        response = self.request_override(self.manager_a, task=free, assigned_to_id=str(self.owner_a.id))
        self.assertEqual(response.status_code, 400)

    def test_approving_applies_the_assignment(self):
        request_id = self.request_override(self.manager_a).json()['data']['request']['id']
        response = self.decide(self.owner_a, request_id, 'approve', comment='Agreed')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Task.objects.get(id=self.task.id).assigned_to_id, self.member_a.id)
        self.assertEqual(ApprovalRequest.objects.get(id=request_id).status, 'approved')

    def test_declining_leaves_the_task_unassigned(self):
        request_id = self.request_override(self.manager_a).json()['data']['request']['id']
        self.assertEqual(self.decide(self.owner_a, request_id, 'decline', 'No').status_code, 200)
        self.assertIsNone(Task.objects.get(id=self.task.id).assigned_to_id)
        self.assertEqual(ApprovalRequest.objects.get(id=request_id).status, 'denied')

    def test_deciding_twice_is_refused(self):
        request_id = self.request_override(self.manager_a).json()['data']['request']['id']
        self.decide(self.owner_a, request_id, 'approve')
        self.assertEqual(self.decide(self.owner_a, request_id, 'approve').status_code, 400)

    def test_a_member_without_manage_cannot_decide(self):
        request_id = self.request_override(self.manager_a).json()['data']['request']['id']
        self.assertEqual(self.decide(self.member_a, request_id, 'approve').status_code, 403)

    def test_cross_tenant_decision_is_a_404(self):
        request_id = self.request_override(self.manager_a).json()['data']['request']['id']
        self.assertEqual(self.decide(self.owner_b, request_id, 'approve').status_code, 404)

    def test_approval_re_checks_rather_than_trusting_the_payload(self):
        """The workload is read again at decision time, so an override
        approved a day later cannot apply a number that was true yesterday."""
        request_id = self.request_override(self.manager_a).json()['data']['request']['id']
        Task.objects.filter(assigned_to=self.member_a).update(status=Task.STATUS.DONE)
        response = self.decide(self.owner_a, request_id, 'approve')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Task.objects.get(id=self.task.id).assigned_to_id, self.member_a.id)

    def test_the_queue_lists_only_this_companys_requests(self):
        self.request_override(self.manager_a)
        response = self.client.get('/api/v1/workload-overrides/', **auth_header(self.owner_b))
        self.assertEqual(response.json()['data']['results'], [])


class ConcurrentAssignmentTests(TransactionTestCase):
    """Two managers assigning the same person at the same moment.

    A ``TransactionTestCase`` rather than the usual ``TestCase``: the whole
    point is two real connections committing, which the outer transaction a
    normal test case wraps every test in would hide.
    """

    reset_sequences = True

    def setUp(self):
        from company.models import Company, Sector

        sector = Sector.objects.create(name='Software')
        self.owner = User.objects.create_user(email='o@example.com', username='o', password=PASSWORD)
        self.company = Company.objects.create(name='Race Co', owner=self.owner, sector=sector)
        CompanyUserProfile.objects.create(
            user=self.owner, company=self.company, role=CompanyUserProfile.Role.Owner,
        )
        self.worker = User.objects.create_user(email='w@example.com', username='w', password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=self.worker, company=self.company, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        self.project = Project.objects.create(
            title='Race', company=self.company, created_by=self.owner, current_owner=self.owner,
            deadline=timezone.now() + timedelta(days=365), status=Project.STATUS.ACTIVE,
        )
        AssignmentPolicy.objects.create(
            company=self.company, enabled=True, max_active_tasks=1,
            enforcement='block', override_roles=[],
        )

    def make_task(self):
        return Task.objects.create(
            project=self.project, title='Work', deadline=timezone.now() + timedelta(days=5),
            created_by=self.owner,
        )

    def test_two_simultaneous_assignments_cannot_both_pass_the_cap(self):
        import threading

        from django.db import connections

        from workforce.workload import guarded_write_sync

        tasks = [self.make_task(), self.make_task()]
        results, errors = [], []

        def attempt(task):
            try:
                def apply():
                    task.assigned_to = self.worker
                    task.save(update_fields=['assigned_to', 'updated_at'])
                    return task

                # Preloaded so the worker thread never traverses project.company
                # while holding the row lock.
                task.project = self.project
                task.project.company = self.company
                decision, applied = guarded_write_sync(self.company, self.worker, apply)
                results.append(applied is not None)
            except Exception as error:  # noqa: BLE001 -- reported, not swallowed
                errors.append(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(task,)) for task in tasks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        # The cap is 1, so exactly one of the two may land.
        self.assertEqual(sum(results), 1, f'expected one winner, got {results}')
        self.assertEqual(Task.objects.filter(assigned_to=self.worker).count(), 1)
