"""Audit trail: that it records what it claims to, and that nothing can
unrecord it.

The second half matters more than the first. An audit table that any code path
can quietly update or delete is not evidence of anything, so append-only is
tested at every layer that could break it -- the instance, the manager, and the
Django admin -- rather than trusted to convention.
"""

import uuid
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase
from django.utils import timezone
from projects_and_tasks import services as project_services
from projects_and_tasks.models import Project, Task, TaskApproval
from users import services as user_services
from users.models import CompanyUserProfile, User

from audit.admin import AuditEventAdmin
from audit.models import AppendOnly, AuditAction, AuditEvent
from audit.services import _json_safe, record_event


class RecordEventTests(TwoCompanyTestCase):
    def test_writes_one_row_carrying_the_change(self):
        event = record_event(
            company=self.company_a, actor=self.owner_a, action=AuditAction.MEMBER_ROLE_CHANGED,
            target=self.member_a, before={'role': 'DM'}, after={'role': 'DL'}, reason='promoted',
        )
        self.assertEqual(AuditEvent.objects.count(), 1)
        self.assertEqual(event.company_id, self.company_a.id)
        self.assertEqual(event.actor_id, self.owner_a.id)
        self.assertEqual(event.action, AuditAction.MEMBER_ROLE_CHANGED)
        self.assertEqual(event.target_type, 'users.user')
        self.assertEqual(event.target_id, self.member_a.id)
        self.assertEqual(event.before, {'role': 'DM'})
        self.assertEqual(event.after, {'role': 'DL'})
        self.assertEqual(event.reason, 'promoted')

    def test_target_reference_survives_the_target_being_deleted(self):
        """The whole point of storing a loose reference instead of a foreign
        key: the row still says what happened to a row that no longer exists."""
        doomed = User.objects.create_user(email='gone@example.com', username='gone', password='Kx9#mQ2vLp8Z')
        target_id = doomed.id
        record_event(
            company=self.company_a, actor=self.owner_a, action=AuditAction.MEMBER_REMOVED, target=doomed,
        )
        doomed.delete()
        event = AuditEvent.objects.get()
        self.assertEqual(event.target_id, target_id)
        self.assertEqual(event.target_type, 'users.user')

    def test_request_id_defaults_to_the_current_correlation_id(self):
        """Callers never pass request_id; it comes from the middleware, which
        is what makes an audit row line up with the request's log lines."""
        response = self.client.get('/api/v1/projects/', **auth_header(self.owner_a), HTTP_X_REQUEST_ID='abc123')
        self.assertEqual(response.status_code, 200)
        # Outside a request the contextvar default applies rather than blowing up.
        event = record_event(
            company=self.company_a, actor=self.owner_a, action=AuditAction.MEMBER_REMOVED, target=self.member_a,
        )
        self.assertEqual(event.request_id, '-')

    def test_reason_defaults_to_empty_not_null(self):
        event = record_event(
            company=self.company_a, actor=self.owner_a, action=AuditAction.MEMBER_REMOVED, target=self.member_a,
        )
        self.assertEqual(event.reason, '')

    def test_actor_may_be_absent_for_a_system_action(self):
        event = record_event(
            company=self.company_a, actor=None, action=AuditAction.MEMBER_DEACTIVATED, target=self.member_a,
        )
        self.assertIsNone(event.actor_id)


class JsonSafeTests(TestCase):
    def test_coerces_values_json_cannot_hold(self):
        moment = timezone.now()
        identifier = uuid.uuid4()
        self.assertEqual(_json_safe(moment), moment.isoformat())
        self.assertEqual(_json_safe(identifier), str(identifier))
        self.assertEqual(_json_safe(timedelta(hours=2)), 7200.0)
        self.assertEqual(_json_safe({'at': moment}), {'at': moment.isoformat()})
        self.assertEqual(_json_safe([identifier]), [str(identifier)])

    def test_a_model_instance_is_reduced_to_its_identity(self):
        """Never a full object dump: an audit row that copies whole records
        becomes a second, unmanaged store of data that was deleted elsewhere
        for a reason."""
        user = User.objects.create_user(email='x@example.com', username='x', password='Kx9#mQ2vLp8Z')
        self.assertEqual(_json_safe(user), str(user.id))

    def test_passes_through_primitives_unchanged(self):
        primitives = {'a': 1, 'b': True, 'c': None, 'd': 'text'}
        self.assertEqual(_json_safe(primitives), primitives)


class AppendOnlyTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.event = record_event(
            company=self.company_a, actor=self.owner_a, action=AuditAction.MEMBER_ROLE_CHANGED,
            target=self.member_a, before={'role': 'DM'}, after={'role': 'DL'},
        )

    def test_an_existing_row_cannot_be_saved_again(self):
        self.event.reason = 'rewritten'
        with self.assertRaises(AppendOnly):
            self.event.save()

    def test_a_row_cannot_be_deleted(self):
        with self.assertRaises(AppendOnly):
            self.event.delete()

    def test_the_manager_cannot_bulk_update(self):
        with self.assertRaises(AppendOnly):
            AuditEvent.objects.filter(id=self.event.id).update(reason='rewritten')

    def test_the_manager_cannot_bulk_delete(self):
        with self.assertRaises(AppendOnly):
            AuditEvent.objects.filter(id=self.event.id).delete()

    def test_the_row_is_unchanged_after_every_attempt(self):
        for attempt in (
            lambda: self.event.save(),
            lambda: self.event.delete(),
            lambda: AuditEvent.objects.all().update(reason='x'),
            lambda: AuditEvent.objects.all().delete(),
        ):
            with self.assertRaises(AppendOnly):
                attempt()
        stored = AuditEvent.objects.get(id=self.event.id)
        self.assertEqual(stored.reason, '')
        self.assertEqual(stored.after, {'role': 'DL'})

    def test_deleting_the_company_still_cascades(self):
        """The one deliberate exception. A tenant's trail must not outlive the
        tenant, and a deletion request has to be satisfiable -- Django's
        collector issues its own SQL and does not go through the manager."""
        self.company_a.delete()
        self.assertFalse(AuditEvent.objects.filter(id=self.event.id).exists())

    def test_the_admin_grants_no_write_permission(self):
        admin = AuditEventAdmin(AuditEvent, AdminSite())
        request = RequestFactory().get('/admin/')
        request.user = self.owner_a
        self.assertFalse(admin.has_add_permission(request))
        self.assertFalse(admin.has_change_permission(request, self.event))
        self.assertFalse(admin.has_delete_permission(request, self.event))


class AuditedMutationTests(TwoCompanyTestCase):
    """Each mutation the brief names writes exactly one row, with the change in
    it. One test per action, so a regression names the action it broke."""

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.project = Project.objects.create(
            title='Website Revamp', company=self.company_a, department=self.department_a,
            start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )

    def single_event(self):
        self.assertEqual(AuditEvent.objects.count(), 1, 'expected exactly one audit row')
        return AuditEvent.objects.get()

    def test_role_change_is_audited(self):
        profile, error = user_services.update_member_role(
            self.owner_a, self.member_a.id, CompanyUserProfile.Role.DEPARTMENT_LEADER,
        )
        self.assertIsNone(error)
        event = self.single_event()
        self.assertEqual(event.action, AuditAction.MEMBER_ROLE_CHANGED)
        self.assertEqual(event.before, {'role': CompanyUserProfile.Role.DEPARTMENT_MEMBER})
        self.assertEqual(event.after, {'role': CompanyUserProfile.Role.DEPARTMENT_LEADER})
        self.assertEqual(event.target_id, profile.id)

    def test_a_role_change_that_changes_nothing_is_not_audited(self):
        """The service returns early when the role already matches. An audit
        trail full of no-op rows is an audit trail nobody reads."""
        _, error = user_services.update_member_role(
            self.owner_a, self.member_a.id, CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        self.assertIsNone(error)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_deactivation_and_reactivation_are_audited_separately(self):
        user_services.set_member_active_status(self.owner_a, self.member_a.id, False)
        user_services.set_member_active_status(self.owner_a, self.member_a.id, True)
        actions = list(AuditEvent.objects.order_by('created_at').values_list('action', flat=True))
        self.assertEqual(actions, [AuditAction.MEMBER_DEACTIVATED, AuditAction.MEMBER_REACTIVATED])

    def test_setting_the_same_active_status_twice_is_audited_once(self):
        user_services.set_member_active_status(self.owner_a, self.member_a.id, False)
        user_services.set_member_active_status(self.owner_a, self.member_a.id, False)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_member_removal_is_audited_against_the_user(self):
        result, error = user_services.remove_member(self.owner_a, self.member_a.id)
        self.assertIsNone(error, result)
        event = self.single_event()
        self.assertEqual(event.action, AuditAction.MEMBER_REMOVED)
        self.assertEqual(event.target_type, 'users.user')
        self.assertEqual(event.target_id, self.member_a.id)
        self.assertEqual(event.before['role'], CompanyUserProfile.Role.DEPARTMENT_MEMBER)
        self.assertIsNone(event.after)

    def test_ownership_transfer_is_audited(self):
        _, error = async_to_sync(project_services.transfer_project_ownership)(
            self.owner_a, self.project, self.member_a.id,
        )
        self.assertIsNone(error)
        event = self.single_event()
        self.assertEqual(event.action, AuditAction.PROJECT_OWNERSHIP_TRANSFERRED)
        self.assertEqual(event.target_type, 'projects_and_tasks.project')
        self.assertEqual(event.before, {'current_owner': str(self.owner_a.id)})
        self.assertEqual(event.after, {'current_owner': str(self.member_a.id)})

    def test_transferring_to_the_current_owner_is_not_audited(self):
        _, error = async_to_sync(project_services.transfer_project_ownership)(
            self.owner_a, self.project, self.owner_a.id,
        )
        self.assertIsNone(error)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def submitted_task(self):
        task = Task.objects.create(
            project=self.project, title='Ship it', created_by=self.owner_a, assigned_to=self.member_a,
            status=Task.STATUS.IN_PROGRESS, deadline=self.project.deadline - timedelta(days=1),
        )
        approval, error = async_to_sync(project_services.submit_task_for_approval)(
            self.member_a, task, links=['https://example.com/evidence'],
        )
        self.assertIsNone(error)
        return task, approval

    def test_approval_is_audited(self):
        task, _ = self.submitted_task()
        _, error = async_to_sync(project_services.approve_task)(self.owner_a, task)
        self.assertIsNone(error)
        event = self.single_event()
        self.assertEqual(event.action, AuditAction.TASK_APPROVED)
        self.assertEqual(event.target_type, 'projects_and_tasks.task')
        self.assertEqual(event.after['status'], Task.STATUS.DONE)

    def test_rejection_is_audited_without_the_rejection_comment(self):
        """The comment is visible only to the submitter. Copying it into a
        company-scoped audit row would route around that the day the audit
        trail grows a UI."""
        task, _ = self.submitted_task()
        secret = 'this critique is for the submitter alone'
        _, error = async_to_sync(project_services.reject_task_approval)(self.owner_a, task, secret)
        self.assertIsNone(error)
        event = self.single_event()
        self.assertEqual(event.action, AuditAction.TASK_APPROVAL_REJECTED)
        serialized = f'{event.before} {event.after} {event.reason}'
        self.assertNotIn(secret, serialized)
        self.assertEqual(
            TaskApproval.objects.get(task=task).rejection_comment, secret,
            'the comment must still reach the submitter through the approval row',
        )

    def test_audit_rows_are_scoped_to_the_company_that_owns_the_change(self):
        user_services.update_member_role(self.owner_a, self.member_a.id, CompanyUserProfile.Role.DEPARTMENT_LEADER)
        self.assertEqual(AuditEvent.objects.filter(company=self.company_a).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(company=self.company_b).count(), 0)
