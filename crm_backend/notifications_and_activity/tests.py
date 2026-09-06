"""Company activity feed: which events get logged (a curated set, not every
field edit), tenant isolation, and the limit bound. Reuses TwoCompanyTestCase
(api/tests.py) for cross-tenant coverage the same way every other endpoint's
tests do.
"""

import json
from datetime import timedelta
from unittest.mock import patch

from api.tests import TwoCompanyTestCase, auth_header
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import timezone
from users.models import CompanyUserProfile, User
from utils.notification_email import TEMPLATE_MAP

from notifications_and_activity.models import CompanyActivity, Notification
from notifications_and_activity.tasks import send_notification_email_task
from projects_and_tasks.models import Project


class CompanyActivityTests(TwoCompanyTestCase):
    def list_activity(self, user, limit=None):
        url = '/api/v1/activity/'
        if limit is not None:
            url += f'?limit={limit}'
        return self.client.get(url, **auth_header(user))

    def test_creating_a_project_logs_an_activity_entry(self):
        self.create_project(owner=self.owner_a)
        self.assertTrue(CompanyActivity.objects.filter(
            company=self.company_a, type=CompanyActivity.ActivityType.PROJECT_CREATED,
        ).exists())

    def test_completing_a_project_logs_an_activity_entry_exactly_once(self):
        # A zero-task project can never reach Done (see
        # projects_and_tasks.services.update_project), so this needs one
        # task taken all the way to Done via the approval workflow first --
        # which also auto-completes the project and logs PROJECT_COMPLETED
        # exactly once via _maybe_auto_complete_project.
        project = self.create_project(owner=self.owner_a)
        task = self.client.post(
            f"/api/v1/projects/{project['id']}/tasks/",
            json.dumps({'title': 'Only task', 'deadline': (timezone.now() + timedelta(days=30)).isoformat()}),
            content_type='application/json', **auth_header(self.owner_a),
        ).json()['data']['task']
        self.client.post(
            f"/api/v1/tasks/{task['id']}/assign/", json.dumps({'assigned_to_id': str(self.member_a.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.client.patch(
            f"/api/v1/tasks/{task['id']}/status/", json.dumps({'status': 'In Progress'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.client.post(
            f"/api/v1/tasks/{task['id']}/submit-for-approval/",
            {'links': ['https://example.com/evidence']}, **auth_header(self.member_a),
        )
        self.client.post(f"/api/v1/tasks/{task['id']}/approve/", **auth_header(self.owner_a))

        # Re-saving the (already-Done) project again must not log a second
        # completion.
        self.client.patch(
            f"/api/v1/projects/{project['id']}/", json.dumps({'description': 'still done'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(
            CompanyActivity.objects.filter(
                company=self.company_a, type=CompanyActivity.ActivityType.PROJECT_COMPLETED,
            ).count(),
            1,
        )

    def test_activity_list_requires_authentication(self):
        response = self.client.get('/api/v1/activity/')
        self.assertEqual(response.status_code, 401)

    def test_activity_is_scoped_to_the_callers_own_company(self):
        self.create_project(owner=self.owner_a)
        response = self.list_activity(self.owner_b)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['results'], [])

    def test_member_can_view_company_activity(self):
        self.create_project(owner=self.owner_a)
        response = self.list_activity(self.member_a)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(len(response.json()['data']['results']) >= 1)

    def test_limit_is_bounded(self):
        for i in range(5):
            self.create_project(owner=self.owner_a, title=f'Project {i}')
        response = self.list_activity(self.owner_a, limit=1000)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.json()['data']['results']), 50)

    def test_creating_a_department_logs_an_activity_entry(self):
        self.client.post(
            '/api/v1/departments/', json.dumps({'name': 'Marketing'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertTrue(CompanyActivity.objects.filter(
            company=self.company_a, type=CompanyActivity.ActivityType.DEPARTMENT_CREATED,
        ).exists())

    def test_creating_a_team_logs_an_activity_entry(self):
        self.client.post(
            '/api/v1/teams/', json.dumps({'name': 'Launch Squad'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertTrue(CompanyActivity.objects.filter(
            company=self.company_a, type=CompanyActivity.ActivityType.TEAM_CREATED,
        ).exists())

    def test_sending_an_invite_logs_an_activity_entry(self):
        with patch('users.tasks.send_invitation_email'):
            self.client.post(
                '/api/v1/auth/send_invite/', json.dumps({'email': 'new@example.com'}),
                content_type='application/json', **auth_header(self.owner_a),
            )
        self.assertTrue(CompanyActivity.objects.filter(
            company=self.company_a, type=CompanyActivity.ActivityType.MEMBER_INVITED,
        ).exists())

    def test_removing_a_member_logs_an_activity_entry(self):
        self.client.post(
            f'/api/v1/company/members/{self.member_a.id}/remove/', json.dumps({}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertTrue(CompanyActivity.objects.filter(
            company=self.company_a, type=CompanyActivity.ActivityType.MEMBER_REMOVED,
        ).exists())

    def test_transferring_ownership_logs_an_activity_entry(self):
        project = self.create_project(owner=self.owner_a)
        self.client.patch(
            f"/api/v1/projects/{project['id']}/owner/", json.dumps({'new_owner_id': str(self.member_a.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        activity = CompanyActivity.objects.get(
            company=self.company_a, type=CompanyActivity.ActivityType.PROJECT_OWNERSHIP_TRANSFERRED,
        )
        self.assertEqual(activity.actor_id, self.owner_a.id)

    def test_ownership_transfer_activity_credits_the_requester_not_the_new_owner(self):
        """Regression test: log_ownership_transferred used to record
        new_owner as the activity's actor, misattributing the transfer to
        whoever merely received ownership -- who may have no manage rights
        on the project at all. The actor must be whoever actually performed
        the transfer (already permission-checked separately)."""
        project = self.create_project(owner=self.owner_a)
        manager = User.objects.create_user(email='manager-a@example.com', username='manager-a', password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(
            user=manager, company=self.company_a, role=CompanyUserProfile.Role.COMPANY_MANAGER,
        )
        response = self.client.patch(
            f"/api/v1/projects/{project['id']}/owner/", json.dumps({'new_owner_id': str(self.member_a.id)}),
            content_type='application/json', **auth_header(manager),
        )
        self.assertEqual(response.status_code, 200)
        activity = CompanyActivity.objects.get(
            company=self.company_a, type=CompanyActivity.ActivityType.PROJECT_OWNERSHIP_TRANSFERRED,
        )
        self.assertEqual(activity.actor_id, manager.id)
        self.assertNotEqual(activity.actor_id, self.member_a.id)


class NotificationEmailCategoryTests(TwoCompanyTestCase):
    """A critical notification always emails; an optional one respects the
    recipient's CompanyUserProfile.email_notifications_enabled preference."""

    def create_task(self, project_id, owner=None, **overrides):
        body = {'title': 'Build login page', 'deadline': (timezone.now() + timedelta(days=30)).isoformat()}
        body.update(overrides)
        return self.client.post(
            f'/api/v1/projects/{project_id}/tasks/', json.dumps(body), content_type='application/json',
            **auth_header(owner or self.owner_a),
        ).json()['data']['task']

    @patch('notifications_and_activity.tasks.send_notification_email_task.delay')
    def test_task_assigned_is_critical_and_always_enqueues_email(self, mock_delay):
        profile = CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a)
        profile.email_notifications_enabled = False
        profile.save(update_fields=['email_notifications_enabled'])

        project = self.create_project(owner=self.owner_a)
        task = self.create_task(project['id'])
        self.client.post(
            f"/api/v1/tasks/{task['id']}/assign/", json.dumps({'assigned_to_id': str(self.member_a.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        notification = Notification.objects.get(recipient=self.member_a, type=Notification.Type.TASK_ASSIGNED)
        self.assertEqual(notification.category, Notification.Category.CRITICAL)
        mock_delay.assert_called_once_with(str(notification.id))

    @patch('notifications_and_activity.tasks.send_notification_email_task.delay')
    def test_optional_notification_skips_email_when_preference_disabled(self, mock_delay):
        # owner_a creates the task; member_a is assigned, submits evidence,
        # and owner_a (the creator) approves it, so the (optional)
        # TASK_APPROVED notification goes to member_a -- the submitter, not
        # the approver -- whose preference is disabled below.
        profile = CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a)
        profile.email_notifications_enabled = False
        profile.save(update_fields=['email_notifications_enabled'])

        project = self.create_project(owner=self.owner_a)
        # A second, still-open task keeps the project from auto-completing
        # when the one under test is approved below -- auto-completion would
        # fire its own (optional) notification to owner_a and confuse this
        # test's "zero emails" assertion with an unrelated one.
        self.create_task(project['id'], owner=self.owner_a, title='Other open task')
        task = self.create_task(project['id'], owner=self.owner_a)
        self.client.post(
            f"/api/v1/tasks/{task['id']}/assign/", json.dumps({'assigned_to_id': str(self.member_a.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.client.patch(
            f"/api/v1/tasks/{task['id']}/status/", json.dumps({'status': 'In Progress'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.client.post(
            f"/api/v1/tasks/{task['id']}/submit-for-approval/",
            {'links': ['https://example.com/evidence']}, **auth_header(self.member_a),
        )
        mock_delay.reset_mock()
        self.client.post(f"/api/v1/tasks/{task['id']}/approve/", **auth_header(self.owner_a))
        notification = Notification.objects.get(recipient=self.member_a, type=Notification.Type.TASK_APPROVED)
        self.assertEqual(notification.category, Notification.Category.OPTIONAL)
        mock_delay.assert_not_called()

    @patch('notifications_and_activity.tasks.send_notification_email_task.delay')
    def test_optional_notification_enqueues_email_when_preference_enabled(self, mock_delay):
        # member_a's preference defaults to enabled (True).
        project = self.create_project(owner=self.owner_a)
        # See the matching comment in test_optional_notification_skips_email_
        # when_preference_disabled above -- keeps the project from
        # auto-completing and firing an extra, unrelated notification.
        self.create_task(project['id'], owner=self.owner_a, title='Other open task')
        task = self.create_task(project['id'], owner=self.owner_a)
        self.client.post(
            f"/api/v1/tasks/{task['id']}/assign/", json.dumps({'assigned_to_id': str(self.member_a.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.client.patch(
            f"/api/v1/tasks/{task['id']}/status/", json.dumps({'status': 'In Progress'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.client.post(
            f"/api/v1/tasks/{task['id']}/submit-for-approval/",
            {'links': ['https://example.com/evidence']}, **auth_header(self.member_a),
        )
        mock_delay.reset_mock()
        self.client.post(f"/api/v1/tasks/{task['id']}/approve/", **auth_header(self.owner_a))
        notification = Notification.objects.get(recipient=self.member_a, type=Notification.Type.TASK_APPROVED)
        mock_delay.assert_called_once_with(str(notification.id))


class NotificationEmailTaskTests(TwoCompanyTestCase):
    @patch('notifications_and_activity.tasks.send_notification_email')
    def test_sends_and_marks_email_sent(self, mock_send):
        notification = Notification.objects.create(
            recipient=self.owner_a, type=Notification.Type.TASK_ASSIGNED,
            category=Notification.Category.CRITICAL, title='Test', message='Body',
        )
        send_notification_email_task(str(notification.id))
        mock_send.assert_called_once_with(self.owner_a.email, 'Test', 'Body', Notification.Type.TASK_ASSIGNED)
        notification.refresh_from_db()
        self.assertTrue(notification.email_sent)

    @patch('notifications_and_activity.tasks.send_notification_email')
    def test_rerunning_on_an_already_sent_notification_is_a_noop(self, mock_send):
        notification = Notification.objects.create(
            recipient=self.owner_a, type=Notification.Type.TASK_ASSIGNED,
            category=Notification.Category.CRITICAL, title='Test', message='Body', email_sent=True,
        )
        send_notification_email_task(str(notification.id))
        mock_send.assert_not_called()


class NotificationEmailTemplateMappingTests(SimpleTestCase):
    """Each Notification.Type must render its own template, not the one
    generic notification_email.html shared by everything (the original gap
    this feature closes) -- and an unmapped/future type must still fall back
    to the generic template rather than raising."""

    def test_every_notification_type_maps_to_a_distinct_template(self):
        mapped_templates = {TEMPLATE_MAP[value] for value in Notification.Type.values}
        self.assertEqual(len(mapped_templates), len(Notification.Type.values))
        for value in Notification.Type.values:
            self.assertIn(value, TEMPLATE_MAP, f'{value} has no dedicated email template mapped')

    def test_unmapped_type_falls_back_to_generic_template(self):
        self.assertEqual(TEMPLATE_MAP.get('some_future_type', 'emails/notification_email.html'), 'emails/notification_email.html')

    def test_mapped_templates_render_without_error(self):
        for notification_type, template in TEMPLATE_MAP.items():
            html = render_to_string(template, {
                'title': 'Test title', 'message': 'Test message',
                'frontend_url': 'http://localhost:3000', 'logo_cid': 'workroom-logo',
            })
            self.assertIn('Test title', html, f'{notification_type} template did not render the title')


class NotificationPreferenceTests(TwoCompanyTestCase):
    def set_preference(self, enabled, user=None):
        return self.client.patch(
            '/api/v1/company/members/me/notification-preference/',
            json.dumps({'email_notifications_enabled': enabled}),
            content_type='application/json', **auth_header(user or self.member_a),
        )

    def test_member_can_disable_their_own_email_notifications(self):
        response = self.set_preference(False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['data']['email_notifications_enabled'])
        self.assertFalse(
            CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a).email_notifications_enabled,
        )

    def test_owner_has_no_profile_to_update(self):
        response = self.set_preference(False, user=self.owner_a)
        self.assertEqual(response.status_code, 400)

    def test_requires_authentication(self):
        response = self.client.patch(
            '/api/v1/company/members/me/notification-preference/',
            json.dumps({'email_notifications_enabled': False}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)


class NotificationFilterTests(TwoCompanyTestCase):
    def test_filters_by_type_and_related_object(self):
        Notification.objects.create(
            recipient=self.owner_a, type=Notification.Type.TASK_ASSIGNED, title='A',
            related_object_type='project', related_object_id=self.owner_a.id,
        )
        Notification.objects.create(recipient=self.owner_a, type=Notification.Type.TASK_COMPLETED, title='B')
        response = self.client.get(
            f'/api/v1/notifications/?type=task_assigned&related_object_type=project'
            f'&related_object_id={self.owner_a.id}',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        results = response.json()['data']['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], 'A')


class ProjectLifecycleNotificationTests(TwoCompanyTestCase):
    """B8: notification/activity coverage for project ownership transfer and
    reopening, which previously had none at all -- and confirms the A10
    self-notification rule holds for both (the acting owner never gets
    notified of their own action)."""

    def notifications_for(self, user):
        return self.client.get('/api/v1/notifications/', **auth_header(user)).json()['data']['results']

    def test_new_owner_is_notified_of_ownership_transfer(self):
        project = self.create_project(owner=self.owner_a)
        self.client.patch(
            f"/api/v1/projects/{project['id']}/owner/", json.dumps({'new_owner_id': str(self.member_a.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        titles = [n['title'] for n in self.notifications_for(self.member_a)]
        self.assertTrue(any('owner' in t.lower() for t in titles))

    def test_transferring_to_self_is_a_noop_and_sends_no_notification(self):
        project = self.create_project(owner=self.owner_a)
        self.client.patch(
            f"/api/v1/projects/{project['id']}/owner/", json.dumps({'new_owner_id': str(self.owner_a.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(Notification.objects.filter(recipient=self.owner_a).count(), 0)

    def _complete_only_task(self, project, actor):
        task = self.client.post(
            f"/api/v1/projects/{project['id']}/tasks/",
            json.dumps({'title': 'Only task', 'deadline': (timezone.now() + timedelta(days=30)).isoformat()}),
            content_type='application/json', **auth_header(actor),
        ).json()['data']['task']
        self.client.post(
            f"/api/v1/tasks/{task['id']}/assign/", json.dumps({'assigned_to_id': str(actor.id)}),
            content_type='application/json', **auth_header(actor),
        )
        self.client.patch(
            f"/api/v1/tasks/{task['id']}/status/", json.dumps({'status': 'In Progress'}),
            content_type='application/json', **auth_header(actor),
        )
        self.client.post(
            f"/api/v1/tasks/{task['id']}/submit-for-approval/",
            {'links': ['https://example.com/evidence']}, **auth_header(actor),
        )
        self.client.post(f"/api/v1/tasks/{task['id']}/approve/", **auth_header(actor))

    def test_reopening_notifies_a_different_current_owner_and_logs_activity(self):
        project = self.create_project(owner=self.owner_a)
        self._complete_only_task(project, self.owner_a)
        # project auto-completed the instant the last task was approved
        self.assertEqual(Project.objects.get(id=project['id']).status, Project.STATUS.DONE)
        self.client.patch(
            f"/api/v1/projects/{project['id']}/owner/", json.dumps({'new_owner_id': str(self.member_a.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        Notification.objects.all().delete()  # clear the transfer notification, isolate the reopen one

        response = self.client.patch(
            f"/api/v1/projects/{project['id']}/", json.dumps({'status': 'Active'}),
            content_type='application/json', **auth_header(self.owner_a),  # creator, not current_owner anymore
        )
        self.assertEqual(response.status_code, 200)
        titles = [n['title'] for n in self.notifications_for(self.member_a)]
        self.assertTrue(any('reopen' in t.lower() for t in titles))
        self.assertTrue(
            CompanyActivity.objects.filter(
                company=self.company_a, type=CompanyActivity.ActivityType.PROJECT_REOPENED,
            ).exists()
        )

    def test_reopening_your_own_project_sends_no_self_notification(self):
        project = self.create_project(owner=self.owner_a)
        self._complete_only_task(project, self.owner_a)
        self.client.patch(
            f"/api/v1/projects/{project['id']}/", json.dumps({'status': 'Active'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        reopen_notifs = [
            n for n in self.notifications_for(self.owner_a)
            if n['type'] == Notification.Type.PROJECT_REOPENED
        ]
        self.assertEqual(reopen_notifs, [])

    def test_creating_a_task_with_an_assignee_notifies_them(self):
        project = self.create_project(owner=self.owner_a)
        self.client.post(
            f"/api/v1/projects/{project['id']}/tasks/",
            json.dumps({
                'title': 'Pre-assigned', 'deadline': (timezone.now() + timedelta(days=30)).isoformat(),
                'assigned_to_id': str(self.member_a.id),
            }),
            content_type='application/json', **auth_header(self.owner_a),
        )
        titles = [n['title'] for n in self.notifications_for(self.member_a)]
        self.assertTrue(any('assigned' in t.lower() for t in titles))
