"""Who may change what on a task, field by field.

Reaching a task and being allowed to rewrite it were the same question before
this. They are now two:

    description, estimate      the assignee -- how the work gets done
    title, priority, type,     whoever can manage the project -- what the
    department, assignee,      work *is*, and where it sits
    deadline, archive

`created_by` grants neither. It is provenance, the same correction Project's
`created_by` got: 240 of the 1440 characterized combinations previously
granted management of a task on a project the same person could not open.

Two routes that used to go round the rules are closed here as well: a deadline
could be moved through the general update endpoint with no reason and no audit
row, and a task with a pending submission could not be reassigned at all.
"""

import json
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from audit.models import AuditAction, AuditEvent
from django.utils import timezone
from notifications_and_activity.models import Notification
from users.models import CompanyUserProfile, User

from projects_and_tasks import services
from projects_and_tasks.models import Project, ProjectMembership, Task, TaskApproval

PASSWORD = 'Kx9#mQ2vLp8Z'


class TaskWorldMixin:
    """One project, one task, and the four people who might touch it: the
    manager, the assignee, an unrelated member of the same company, and the
    person who created the task but holds nothing else on the project."""

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.project = Project.objects.create(
            title='Website Revamp', company=self.company_a, department=self.department_a,
            visibility=Project.VISIBILITY.COMPANY, start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )
        self.assignee = self._member('assignee')
        self.bystander = self._member('bystander')
        self.task_creator = self._member('task-creator')
        self.task = Task.objects.create(
            project=self.project, title='Ship the landing page', description='Original',
            priority=Task.PRIORITY.MEDIUM, deadline=now + timedelta(days=30),
            created_by=self.task_creator, assigned_to=self.assignee,
        )

    def _member(self, name, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER, department=None):
        user = User.objects.create_user(email=f'{name}@example.com', username=name, password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, department=department or self.department_a, role=role,
        )
        return user

    def patch_task(self, actor, body, task=None):
        task = task or self.task
        return self.client.patch(
            f'/api/v1/tasks/{task.id}/', json.dumps(body),
            content_type='application/json', **auth_header(actor),
        )


class TaskFieldAuthorityTests(TaskWorldMixin, TwoCompanyTestCase):
    """The assignee owns how the work is done; management owns what it is."""

    def test_the_assignee_may_edit_the_description_and_estimate(self):
        response = self.patch_task(self.assignee, {'description': 'Refined approach', 'estimated_time_hours': 4})
        self.assertEqual(response.status_code, 200, response.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.description, 'Refined approach')

    def test_the_assignee_may_not_rename_the_task(self):
        """Renaming changes what the work *is*, which is not the assignee's to
        decide -- it is how a task quietly becomes a different task."""
        response = self.patch_task(self.assignee, {'title': 'Something else entirely'})
        self.assertEqual(response.status_code, 403, response.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, 'Ship the landing page')

    def test_an_assignee_field_bundled_with_a_manage_field_is_refused_whole(self):
        """No partial application. Letting the permitted half through would
        make the outcome of one request depend on which fields it happened to
        carry, and would half-apply an edit the caller sent as one change."""
        response = self.patch_task(
            self.assignee, {'description': 'Refined approach', 'priority': 'high'},
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.description, 'Original')
        self.assertEqual(self.task.priority, Task.PRIORITY.MEDIUM)

    def test_a_manager_may_edit_every_field(self):
        response = self.patch_task(self.owner_a, {'title': 'Ship it', 'priority': 'high', 'description': 'Rescoped'})
        self.assertEqual(response.status_code, 200, response.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, 'Ship it')
        self.assertEqual(self.task.priority, Task.PRIORITY.HIGH)

    def test_a_bystander_who_can_see_the_task_may_not_edit_it(self):
        """Company visibility grants discovery, not authorship."""
        response = self.patch_task(self.bystander, {'description': 'I was here'})
        self.assertEqual(response.status_code, 403, response.content)

    def test_another_companys_owner_cannot_reach_the_task_at_all(self):
        response = self.patch_task(self.owner_b, {'description': 'Cross-tenant'})
        self.assertIn(response.status_code, (403, 404))
        self.task.refresh_from_db()
        self.assertEqual(self.task.description, 'Original')


class TaskCreatorHasNoStandingClaimTests(TaskWorldMixin, TwoCompanyTestCase):
    """`created_by` records who raised the work. It is not authority over it."""

    def test_the_creator_cannot_edit_a_task_they_no_longer_manage(self):
        response = self.patch_task(self.task_creator, {'title': 'Mine again'})
        self.assertEqual(response.status_code, 403, response.content)

    def test_the_creator_cannot_reassign_it(self):
        response = self.client.post(
            f'/api/v1/tasks/{self.task.id}/assign/', json.dumps({'assigned_to_id': str(self.bystander.id)}),
            content_type='application/json', **auth_header(self.task_creator),
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to_id, self.assignee.id)

    def test_the_creator_cannot_archive_it(self):
        response = self.client.delete(f'/api/v1/tasks/{self.task.id}/', **auth_header(self.task_creator))
        self.assertEqual(response.status_code, 403, response.content)
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_deleted)

    def test_a_manager_membership_is_how_a_creator_keeps_authority(self):
        """The replacement for the old implicit grant: name the person, on a
        row someone can see and revoke."""
        ProjectMembership.objects.create(
            project=self.project, user=self.task_creator, role=ProjectMembership.Role.MANAGER,
            added_by=self.owner_a,
        )
        response = self.patch_task(self.task_creator, {'title': 'Mine again'})
        self.assertEqual(response.status_code, 200, response.content)

    def test_manage_without_view_is_gone(self):
        """The 240-row defect, stated directly: a Department Leader of another
        department who created this task can neither see the project nor
        manage the task on it."""
        other_department = self.department_a.__class__.objects.create(name='Sales', company=self.company_a)
        outsider = self._member('other-dl', CompanyUserProfile.Role.DEPARTMENT_LEADER, other_department)
        self.project.visibility = Project.VISIBILITY.PRIVATE
        self.project.save(update_fields=['visibility'])
        self.task.created_by = outsider
        self.task.save(update_fields=['created_by'])

        self.assertFalse(async_to_sync(services.user_can_view_project)(outsider, self.project))
        self.assertFalse(async_to_sync(services.user_can_manage_task)(outsider, self.task))


class TaskDeadlineHasOneRouteTests(TaskWorldMixin, TwoCompanyTestCase):
    """A deadline moves through the endpoint that demands a reason, or not at
    all. The general update path used to be a way round all three of the
    reason, the audit row and the notification."""

    def test_the_general_update_endpoint_refuses_a_deadline(self):
        new_deadline = (timezone.now() + timedelta(days=45)).isoformat()
        response = self.patch_task(self.owner_a, {'deadline': new_deadline})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('change-deadline', response.json()['message'])

    def test_it_is_refused_even_for_someone_who_could_change_it_properly(self):
        """The refusal is about the route, not the person -- otherwise the
        rule would read as a permission check and get 'fixed' by granting
        permission."""
        original = self.task.deadline
        response = self.patch_task(self.owner_a, {'deadline': (original + timedelta(days=1)).isoformat()})
        self.assertEqual(response.status_code, 400)
        self.task.refresh_from_db()
        self.assertEqual(self.task.deadline, original)

    def test_no_audit_row_is_written_by_the_refusal(self):
        before = AuditEvent.objects.filter(action=AuditAction.TASK_DEADLINE_CHANGED).count()
        self.patch_task(self.owner_a, {'deadline': (timezone.now() + timedelta(days=45)).isoformat()})
        after = AuditEvent.objects.filter(action=AuditAction.TASK_DEADLINE_CHANGED).count()
        self.assertEqual(before, after)

    def test_the_dedicated_endpoint_still_works(self):
        response = self.client.post(
            f'/api/v1/tasks/{self.task.id}/change-deadline/',
            json.dumps({
                'deadline': (timezone.now() + timedelta(days=45)).isoformat(),
                'reason': 'Scope grew after the design review',
            }),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)


class ReassignmentVoidsPendingSubmissionTests(TaskWorldMixin, TwoCompanyTestCase):
    """Reassigning a task with a submission waiting closes that submission
    rather than being blocked by it."""

    def setUp(self):
        super().setUp()
        self.task.status = Task.STATUS.IN_REVIEW
        self.task.save(update_fields=['status'])
        self.approval = TaskApproval.objects.create(
            task=self.task, submitted_by=self.assignee, status=TaskApproval.STATUS.PENDING,
        )
        self.successor = self._member('successor')

    def reassign(self, actor=None, to=None):
        return self.client.post(
            f'/api/v1/tasks/{self.task.id}/assign/',
            json.dumps({'assigned_to_id': str((to or self.successor).id)}),
            content_type='application/json', **auth_header(actor or self.owner_a),
        )

    def test_the_reassignment_is_allowed(self):
        """Blocking it was the wrong way round: the usual reason to reassign
        mid-review is that the original assignee is gone or stuck, which is
        exactly when their submission will never be resolved."""
        response = self.reassign()
        self.assertEqual(response.status_code, 200, response.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to_id, self.successor.id)

    def test_the_pending_submission_is_voided_not_rejected(self):
        """Rejected is a judgement on the work. Nobody read this work."""
        self.reassign()
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, TaskApproval.STATUS.VOIDED)

    def test_the_void_records_who_closed_it_and_when(self):
        self.reassign()
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.decided_by_id, self.owner_a.id)
        self.assertIsNotNone(self.approval.decided_at)

    def test_the_task_drops_back_to_in_progress(self):
        """In Review means somebody is waiting on a decision. After this
        nobody is, and leaving it there would wedge the new assignee."""
        self.reassign()
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.STATUS.IN_PROGRESS)

    def test_the_previous_assignee_is_told(self):
        self.reassign()
        notification = Notification.objects.filter(
            recipient=self.assignee, type=Notification.Type.TASK_SUBMISSION_VOIDED,
        ).first()
        self.assertIsNotNone(notification)

    def test_the_new_assignee_is_not_told_about_the_void(self):
        """Only that they have been assigned. The voided submission was
        somebody else's work and is not theirs to see."""
        self.reassign()
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.successor, type=Notification.Type.TASK_SUBMISSION_VOIDED,
            ).exists()
        )

    def test_the_void_is_audited(self):
        self.reassign()
        event = AuditEvent.objects.filter(action=AuditAction.TASK_APPROVAL_VOIDED).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.actor_id, self.owner_a.id)

    def test_the_assignee_change_is_audited(self):
        self.reassign()
        event = AuditEvent.objects.filter(action=AuditAction.TASK_ASSIGNEE_CHANGED).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.before['assigned_to'], str(self.assignee.id))
        self.assertEqual(event.after['assigned_to'], str(self.successor.id))

    def test_reassigning_to_the_same_person_voids_nothing(self):
        """A no-op must not close a live submission out from under someone."""
        response = self.reassign(to=self.assignee)
        self.assertEqual(response.status_code, 200, response.content)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, TaskApproval.STATUS.PENDING)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.STATUS.IN_REVIEW)

    def test_unassigning_also_voids(self):
        response = self.client.post(
            f'/api/v1/tasks/{self.task.id}/assign/', json.dumps({'assigned_to_id': None}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, TaskApproval.STATUS.VOIDED)

    def test_a_voided_submission_does_not_block_the_next_one(self):
        """The already_pending guard counts pending submissions only, so the
        new assignee can submit their own work."""
        self.reassign()
        self.task.refresh_from_db()
        self.assertFalse(
            TaskApproval.objects.filter(task=self.task, status=TaskApproval.STATUS.PENDING).exists()
        )


class ProjectDeadlineHasOneRouteTests(TaskWorldMixin, TwoCompanyTestCase):
    """The same rule as tasks, which the project path was missing.

    WP5 built change-deadline for projects -- MANAGE, required reason, audit
    row, notifications, and a refusal if the new date would strand tasks past
    it -- and `update_project` went on accepting `deadline` directly, making
    all five optional by choosing the other endpoint. WP7a closed that for
    tasks and left it open for projects.
    """

    def patch_project(self, actor, body):
        return self.client.patch(
            f'/api/v1/projects/{self.project.id}/', json.dumps(body),
            content_type='application/json', **auth_header(actor),
        )

    def test_the_general_update_endpoint_refuses_a_deadline(self):
        response = self.patch_project(
            self.owner_a, {'deadline': (timezone.now() + timedelta(days=200)).isoformat()},
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('change-deadline', response.json()['message'])

    def test_the_deadline_is_unchanged(self):
        original = self.project.deadline
        self.patch_project(self.owner_a, {'deadline': (original + timedelta(days=30)).isoformat()})
        self.project.refresh_from_db()
        self.assertEqual(self.project.deadline, original)

    def test_no_audit_row_is_written_by_the_refusal(self):
        before = AuditEvent.objects.filter(action=AuditAction.PROJECT_DEADLINE_CHANGED).count()
        self.patch_project(self.owner_a, {'deadline': (timezone.now() + timedelta(days=200)).isoformat()})
        after = AuditEvent.objects.filter(action=AuditAction.PROJECT_DEADLINE_CHANGED).count()
        self.assertEqual(before, after)

    def test_other_fields_still_update(self):
        """The refusal is about one field, not the endpoint."""
        response = self.patch_project(self.owner_a, {'title': 'Renamed'})
        self.assertEqual(response.status_code, 200, response.content)
        self.project.refresh_from_db()
        self.assertEqual(self.project.title, 'Renamed')

    def test_the_dedicated_endpoint_still_works(self):
        response = self.client.post(
            f'/api/v1/projects/{self.project.id}/change-deadline/',
            json.dumps({
                'deadline': (timezone.now() + timedelta(days=120)).isoformat(),
                'reason': 'Client moved the launch',
            }),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
