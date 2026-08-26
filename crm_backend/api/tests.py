"""Security-critical tests for the Django Ninja API.

Phase 0 baseline: prove the authorization bugs fixed in this phase stay
fixed, per DEVELOPMENT_RULES Rule 12 (test security paths before raw
coverage). Phases 1-5 add project/task/document/AI-generation coverage,
focused on cross-tenant rejection over raw endpoint coverage.
"""

import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from ai_agent.models import AIAssistantQuery, AIGeneratedTask, AIGeneration, AIProjectHealthSummary
from company.models import Company, Sector
from departments_and_teams.models import Department, Team
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from notifications_and_activity.models import Notification
from pages.models import Page, PageFolder
from projects_and_tasks.models import Task
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CompanyUserProfile, PendingInvite, User


def auth_header(user):
    token = RefreshToken.for_user(user).access_token
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


class TwoCompanyTestCase(TestCase):
    """Shared fixture: two companies, each with an owner, so every test can
    assert the other company's owner is rejected."""

    def setUp(self):
        sector = Sector.objects.create(name='Software')
        self.owner_a = User.objects.create_user(
            email='owner-a@example.com', username='owner-a', password='Kx9#mQ2vLp8Z',
        )
        self.company_a = Company.objects.create(name='Company A', owner=self.owner_a, sector=sector)
        self.department_a = Department.objects.create(name='Engineering', company=self.company_a)
        self.member_a = User.objects.create_user(
            email='member-a@example.com', username='member-a', password='Kx9#mQ2vLp8Z',
        )
        CompanyUserProfile.objects.create(
            user=self.member_a, company=self.company_a, department=self.department_a,
            role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )

        self.owner_b = User.objects.create_user(
            email='owner-b@example.com', username='owner-b', password='Kx9#mQ2vLp8Z',
        )
        self.company_b = Company.objects.create(name='Company B', owner=self.owner_b, sector=sector)

    def create_project(self, owner=None, **overrides):
        body = {'title': 'Website Revamp', 'visibility': 'company'}
        body.update(overrides)
        response = self.client.post(
            '/api/v1/projects/', json.dumps(body), content_type='application/json',
            **auth_header(owner or self.owner_a),
        )
        return response.json()['data']['project']


class UnhandledExceptionEnvelopeTests(TwoCompanyTestCase):
    """The catch-all handler (api.api.handle_unexpected_error) must turn any
    uncaught exception into the app's normal response envelope -- never a
    raw traceback -- and never leak the exception message to the client."""

    def test_unexpected_exception_returns_safe_envelope(self):
        with patch('projects_and_tasks.services.list_projects_for_user', side_effect=RuntimeError('boom, contains secret db info')):
            response = self.client.get('/api/v1/projects/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['statusCode'], 500)
        self.assertNotIn('boom', body['message'])
        self.assertNotIn('RuntimeError', body['message'])


class CompanyRegistrationSecurityTests(TestCase):
    def setUp(self):
        self.sector = Sector.objects.create(name='Software')
        self.user = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')

    def test_register_company_requires_authentication(self):
        response = self.client.post(
            '/api/v1/company/register/', {'name': 'Acme', 'sector': self.sector.id}, content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)

    def test_register_company_ignores_client_supplied_owner(self):
        """A client-supplied 'owner' must never override the authenticated user."""
        victim = User.objects.create_user(email='victim@example.com', username='victim', password='Kx9#mQ2vLp8Z')
        response = self.client.post(
            '/api/v1/company/register/',
            {'name': 'Acme', 'sector': self.sector.id, 'owner': victim.id},
            content_type='application/json',
            **auth_header(self.user),
        )
        self.assertEqual(response.status_code, 201)
        company = Company.objects.get(name='Acme')
        self.assertEqual(company.owner_id, self.user.id)

    def test_user_cannot_register_a_second_company(self):
        Company.objects.create(name='First', owner=self.user, sector=self.sector)
        response = self.client.post(
            '/api/v1/company/register/', {'name': 'Second', 'sector': self.sector.id},
            content_type='application/json', **auth_header(self.user),
        )
        self.assertEqual(response.status_code, 400)


class SignInCompanyContextTests(TestCase):
    def test_signin_includes_the_company_creation_date_for_date_boundaries(self):
        sector = Sector.objects.create(name='Software')
        user = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')
        company = Company.objects.create(name='Acme', owner=user, sector=sector)

        response = self.client.post(
            '/api/v1/auth/signin/', json.dumps({'email': user.email, 'password': 'Kx9#mQ2vLp8Z'}),
            content_type='application/json', secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['company_id'], str(company.id))
        self.assertEqual(response.json()['data']['company_created_at'], company.created_at.isoformat())

    def test_signin_department_id_is_null_for_the_owner(self):
        sector = Sector.objects.create(name='Software')
        user = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')
        Company.objects.create(name='Acme', owner=user, sector=sector)

        response = self.client.post(
            '/api/v1/auth/signin/', json.dumps({'email': user.email, 'password': 'Kx9#mQ2vLp8Z'}),
            content_type='application/json', secure=True,
        )

        self.assertIsNone(response.json()['data']['department_id'])

    def test_signin_includes_department_id_for_a_department_scoped_member(self):
        sector = Sector.objects.create(name='Software')
        owner = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')
        company = Company.objects.create(name='Acme', owner=owner, sector=sector)
        department = Department.objects.create(name='Engineering', company=company)
        leader = User.objects.create_user(email='leader@example.com', username='leader', password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(
            user=leader, company=company, department=department,
            role=CompanyUserProfile.Role.DEPARTMENT_LEADER,
        )

        response = self.client.post(
            '/api/v1/auth/signin/', json.dumps({'email': leader.email, 'password': 'Kx9#mQ2vLp8Z'}),
            content_type='application/json', secure=True,
        )

        self.assertEqual(response.json()['data']['department_id'], str(department.id))


class SectorSharingTests(TestCase):
    """Regression test for the Company.sector OneToOneField -> ForeignKey fix."""

    def test_multiple_companies_can_share_a_sector(self):
        sector = Sector.objects.create(name='Finance')
        owner_a = User.objects.create_user(email='a@example.com', username='a', password='Kx9#mQ2vLp8Z')
        owner_b = User.objects.create_user(email='b@example.com', username='b', password='Kx9#mQ2vLp8Z')
        Company.objects.create(name='A Co', owner=owner_a, sector=sector)
        Company.objects.create(name='B Co', owner=owner_b, sector=sector)
        self.assertEqual(sector.companies.count(), 2)


class InvitationRoleTests(TestCase):
    def setUp(self):
        self.sector = Sector.objects.create(name='Software')
        self.owner = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')
        self.company = Company.objects.create(name='Acme', owner=self.owner, sector=self.sector)
        self.outsider = User.objects.create_user(
            email='outsider@example.com', username='outsider', password='Kx9#mQ2vLp8Z',
        )

    def test_invite_cannot_grant_owner_role(self):
        """A company must never gain a second Owner-role profile via invitation."""
        response = self.client.post(
            '/api/v1/auth/send_invite/', {'email': 'new@example.com', 'role': 'Owner'},
            content_type='application/json', **auth_header(self.owner),
        )
        self.assertEqual(response.status_code, 422)

    def test_default_invite_role_is_department_member(self):
        response = self.client.post(
            '/api/v1/auth/send_invite/', {'email': 'new@example.com'},
            content_type='application/json', **auth_header(self.owner),
        )
        self.assertEqual(response.status_code, 200)

    def test_department_leader_invite_requires_a_department(self):
        response = self.client.post(
            '/api/v1/auth/send_invite/', {'email': 'leader@example.com', 'role': 'DL'},
            content_type='application/json', **auth_header(self.owner),
        )
        self.assertEqual(response.status_code, 400)

    def test_user_without_company_cannot_send_invite(self):
        response = self.client.post(
            '/api/v1/auth/send_invite/', {'email': 'new@example.com'},
            content_type='application/json', **auth_header(self.outsider),
        )
        self.assertEqual(response.status_code, 403)


class InvitationTokenSecurityTests(TestCase):
    """utils.tokens: only a hash is ever persisted, and accepting an invite
    must delete the row rather than leave an 'Accepted' one behind."""

    def setUp(self):
        sector = Sector.objects.create(name='Software')
        self.owner = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')
        self.company = Company.objects.create(name='Acme', owner=self.owner, sector=sector)

    def send_invite_and_get_raw_token(self, email='invitee@example.com', **invite_fields):
        payload = {'email': email, **invite_fields}
        with patch('users.tasks.send_invitation_email') as mock_send:
            self.client.post(
                '/api/v1/auth/send_invite/', payload,
                content_type='application/json', **auth_header(self.owner),
            )
        raw_token = mock_send.call_args.args[-1].split('token=')[-1]
        return raw_token

    def accept_payload(self, token):
        return {
            'token': token,
            'password': 'Kx9#mQ2vLp8Z',
            'full_name': 'Invitee Example',
            'profile_picture': SimpleUploadedFile(
                'avatar.png', b'\x89PNG\r\n\x1a\n', content_type='image/png',
            ),
        }

    def test_raw_token_is_never_stored_in_the_database(self):
        raw_token = self.send_invite_and_get_raw_token()
        invite = PendingInvite.objects.get(email='invitee@example.com')
        self.assertNotEqual(invite.token_hash, raw_token)
        self.assertEqual(invite.token_hash, hashlib.sha256(raw_token.encode()).hexdigest())

    def test_accept_invite_rejects_wrong_token(self):
        self.send_invite_and_get_raw_token()
        response = self.client.post(
            '/api/v1/emp/accept_invite/',
            self.accept_payload('not-the-real-token'),
        )
        self.assertEqual(response.status_code, 400)

    def test_accept_invite_deletes_the_pending_invite_row(self):
        raw_token = self.send_invite_and_get_raw_token()
        self.assertEqual(PendingInvite.objects.filter(email='invitee@example.com').count(), 1)
        response = self.client.post(
            '/api/v1/emp/accept_invite/',
            self.accept_payload(raw_token),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(PendingInvite.objects.filter(email='invitee@example.com').count(), 0)

    def test_send_invite_sets_email_sent_true_on_success(self):
        self.send_invite_and_get_raw_token()
        invite = PendingInvite.objects.get(email='invitee@example.com')
        self.assertTrue(invite.email_sent)

    def test_send_invite_marks_email_sent_false_on_delivery_failure(self):
        with patch('users.tasks.send_invitation_email', side_effect=RuntimeError('smtp down')):
            response = self.client.post(
                '/api/v1/auth/send_invite/', {'email': 'invitee@example.com'},
                content_type='application/json', **auth_header(self.owner),
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['data']['email_sent'])
        invite = PendingInvite.objects.get(email='invitee@example.com')
        self.assertFalse(invite.email_sent)

    def test_invite_applies_selected_department_and_role_after_registration(self):
        department = Department.objects.create(name='Design', company=self.company)
        raw_token = self.send_invite_and_get_raw_token(
            email='leader@example.com', department=str(department.id), role='DL',
        )
        invite = PendingInvite.objects.get(email='leader@example.com')
        self.assertEqual(invite.department_id, department.id)
        self.assertEqual(invite.role, CompanyUserProfile.Role.DEPARTMENT_LEADER)

        response = self.client.post('/api/v1/emp/accept_invite/', self.accept_payload(raw_token))
        self.assertEqual(response.status_code, 201)
        profile = CompanyUserProfile.objects.get(user__email='leader@example.com', company=self.company)
        self.assertEqual(profile.department_id, department.id)
        self.assertEqual(profile.role, CompanyUserProfile.Role.DEPARTMENT_LEADER)

    def test_expired_invite_is_deleted_before_a_new_invite_is_issued(self):
        self.send_invite_and_get_raw_token()
        expired = PendingInvite.objects.get(email='invitee@example.com')
        expired.expires_at = timezone.now() - timedelta(seconds=1)
        expired.save(update_fields=['expires_at'])

        replacement_token = self.send_invite_and_get_raw_token()
        replacement = PendingInvite.objects.get(email='invitee@example.com')
        self.assertNotEqual(replacement.id, expired.id)
        self.assertEqual(replacement.token_hash, hashlib.sha256(replacement_token.encode()).hexdigest())
        self.assertEqual(PendingInvite.objects.filter(email='invitee@example.com').count(), 1)

    def test_accepting_an_expired_invite_deletes_the_stale_record(self):
        raw_token = self.send_invite_and_get_raw_token()
        invite = PendingInvite.objects.get(email='invitee@example.com')
        invite.expires_at = timezone.now() - timedelta(seconds=1)
        invite.save(update_fields=['expires_at'])

        response = self.client.post('/api/v1/emp/accept_invite/', self.accept_payload(raw_token))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PendingInvite.objects.filter(id=invite.id).exists())

    def test_workload_exposes_and_streams_a_registered_members_profile_picture(self):
        raw_token = self.send_invite_and_get_raw_token()
        accepted = self.client.post('/api/v1/emp/accept_invite/', self.accept_payload(raw_token))
        self.assertEqual(accepted.status_code, 201)
        user_id = accepted.json()['data']['user']['id']

        workload = self.client.get('/api/v1/analytics/company/members/', **auth_header(self.owner))
        self.assertEqual(workload.status_code, 200)
        member = next(row for row in workload.json()['data']['members'] if row['id'] == user_id)
        self.assertEqual(member['profile_picture_url'], f'/company/members/{user_id}/profile-image/')

        image = self.client.get(f"/api/v1{member['profile_picture_url']}", **auth_header(self.owner))
        self.assertEqual(image.status_code, 200)
        self.assertTrue(image.streaming)


class DefaultDepartmentAndTaskTypeSelectionTests(TwoCompanyTestCase):
    """Regression coverage for a real bug found while adding the Company
    Manager role: these two endpoints hardcoded an owner-only check instead
    of using company.services.get_managed_company like every other
    "manage this company" endpoint, silently rejecting Department Leaders.
    """

    def setUp(self):
        super().setUp()
        from departments_and_teams.models import DefaultDepartment
        from projects_and_tasks.models import DefaultTaskType

        self.default_department = DefaultDepartment.objects.create(name='Engineering', sector=None)
        self.default_task_type = DefaultTaskType.objects.create(name='Bug', sector=None)
        self.dl_a = self._make_department_leader()

    def _make_department_leader(self):
        user = User.objects.create_user(email='dl-a@example.com', username='dl-a', password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, department=self.department_a,
            role=CompanyUserProfile.Role.DEPARTMENT_LEADER,
        )
        return user

    def create_departments(self, user):
        return self.client.post(
            '/api/v1/department/create_departments_from_defaults/',
            json.dumps({'company_id': str(self.company_a.id), 'use_all_default_departments': True}),
            content_type='application/json', **auth_header(user),
        )

    def create_task_types(self, user):
        return self.client.post(
            '/api/v1/default_task_type/default_task_type/',
            json.dumps({'company_id': str(self.company_a.id), 'use_all_default_task_types': True}),
            content_type='application/json', **auth_header(user),
        )

    def test_owner_can_create_departments_from_defaults(self):
        self.assertEqual(self.create_departments(self.owner_a).status_code, 201)

    def test_department_leader_can_create_departments_from_defaults(self):
        self.assertEqual(self.create_departments(self.dl_a).status_code, 201)

    def test_department_member_cannot_create_departments_from_defaults(self):
        self.assertEqual(self.create_departments(self.member_a).status_code, 403)

    def test_other_companys_owner_cannot_create_departments_for_this_company(self):
        self.assertEqual(self.create_departments(self.owner_b).status_code, 403)

    def test_owner_can_create_task_types_from_defaults(self):
        self.assertEqual(self.create_task_types(self.owner_a).status_code, 201)

    def test_department_leader_can_create_task_types_from_defaults(self):
        self.assertEqual(self.create_task_types(self.dl_a).status_code, 201)

    def test_department_member_cannot_create_task_types_from_defaults(self):
        self.assertEqual(self.create_task_types(self.member_a).status_code, 403)


class SignupPasswordValidationTests(TestCase):
    def test_signup_rejects_weak_password(self):
        response = self.client.post(
            '/api/v1/auth/signup/',
            {'email': 'weak@example.com', 'username': 'weak', 'password': 'password'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_signup_accepts_strong_password(self):
        response = self.client.post(
            '/api/v1/auth/signup/',
            {'email': 'strong@example.com', 'username': 'strong', 'password': 'Kx9#mQ2vLp8Z'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)


class ProjectSecurityTests(TwoCompanyTestCase):
    def test_create_project_requires_company_membership(self):
        outsider = User.objects.create_user(email='outsider@example.com', username='outsider', password='Kx9#mQ2vLp8Z')
        response = self.client.post(
            '/api/v1/projects/', json.dumps({'title': 'Ghost Project'}), content_type='application/json',
            **auth_header(outsider),
        )
        self.assertEqual(response.status_code, 400)

    def test_created_project_is_scoped_to_callers_own_company(self):
        project = self.create_project(owner=self.owner_a)
        self.assertEqual(project['company_id'], str(self.company_a.id))

    def test_company_project_hidden_from_other_company(self):
        project = self.create_project(owner=self.owner_a, visibility='company')
        response = self.client.get(f"/api/v1/projects/{project['id']}/", **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)

    def test_company_project_listed_only_for_own_company(self):
        self.create_project(owner=self.owner_a, visibility='company')
        response = self.client.get('/api/v1/projects/', **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['meta']['count'], 0)

    def test_private_project_hidden_from_non_collaborator_company_member(self):
        project = self.create_project(owner=self.owner_a, visibility='private')
        response = self.client.get(f"/api/v1/projects/{project['id']}/", **auth_header(self.member_a))
        self.assertEqual(response.status_code, 403)

    def test_public_project_visible_across_companies(self):
        project = self.create_project(owner=self.owner_a, visibility='public')
        response = self.client.get(f"/api/v1/projects/{project['id']}/", **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 200)

    def test_only_manager_can_update_project(self):
        project = self.create_project(owner=self.owner_a)
        response = self.client.patch(
            f"/api/v1/projects/{project['id']}/", json.dumps({'title': 'Hijacked'}), content_type='application/json',
            **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 403)

    def test_client_supplied_company_field_is_ignored(self):
        """ProjectIn has no company field at all; sending one must have zero effect."""
        response = self.client.post(
            '/api/v1/projects/',
            json.dumps({'title': 'Sneaky', 'company': str(self.company_b.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['data']['project']['company_id'], str(self.company_a.id))


class TaskSecurityTests(TwoCompanyTestCase):
    def create_task(self, project_id, owner=None, **overrides):
        body = {'title': 'Build login page'}
        body.update(overrides)
        response = self.client.post(
            f'/api/v1/projects/{project_id}/tasks/', json.dumps(body), content_type='application/json',
            **auth_header(owner or self.owner_a),
        )
        return response

    def test_outsider_cannot_create_task_in_other_companys_project(self):
        project = self.create_project(owner=self.owner_a)
        response = self.create_task(project['id'], owner=self.owner_b)
        self.assertEqual(response.status_code, 403)

    def test_task_assignment_rejects_non_company_member(self):
        project = self.create_project(owner=self.owner_a)
        task = self.create_task(project['id']).json()['data']['task']
        response = self.client.post(
            f"/api/v1/tasks/{task['id']}/assign/",
            json.dumps({'assigned_to_id': str(self.owner_b.id)}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_task_assignment_accepts_eligible_company_member(self):
        project = self.create_project(owner=self.owner_a)
        task = self.create_task(project['id']).json()['data']['task']
        response = self.client.post(
            f"/api/v1/tasks/{task['id']}/assign/",
            json.dumps({'assigned_to_id': str(self.member_a.id)}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)

    def test_assignee_can_update_status_but_outsider_cannot(self):
        project = self.create_project(owner=self.owner_a)
        task = self.create_task(project['id']).json()['data']['task']
        self.client.post(
            f"/api/v1/tasks/{task['id']}/assign/",
            json.dumps({'assigned_to_id': str(self.member_a.id)}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        ok = self.client.patch(
            f"/api/v1/tasks/{task['id']}/status/", json.dumps({'status': 'In Progress'}), content_type='application/json',
            **auth_header(self.member_a),
        )
        self.assertEqual(ok.status_code, 200)
        forbidden = self.client.patch(
            f"/api/v1/tasks/{task['id']}/status/", json.dumps({'status': 'Done'}), content_type='application/json',
            **auth_header(self.owner_b),
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_invalid_status_value_rejected(self):
        project = self.create_project(owner=self.owner_a)
        task = self.create_task(project['id']).json()['data']['task']
        response = self.client.patch(
            f"/api/v1/tasks/{task['id']}/status/", json.dumps({'status': 'Cancelled'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 422)


class DocumentSecurityTests(TwoCompanyTestCase):
    def test_disallowed_content_type_rejected(self):
        project = self.create_project(owner=self.owner_a)
        upload = SimpleUploadedFile('payload.exe', b'MZ...', content_type='application/x-msdownload')
        response = self.client.post(
            f"/api/v1/projects/{project['id']}/documents/", {'file': upload, 'label': 'binary'},
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_valid_document_upload_and_cross_tenant_download_rejected(self):
        project = self.create_project(owner=self.owner_a)
        upload = SimpleUploadedFile('spec.txt', b'project spec', content_type='text/plain')
        created = self.client.post(
            f"/api/v1/projects/{project['id']}/documents/", {'file': upload, 'label': 'spec'},
            **auth_header(self.owner_a),
        )
        self.assertEqual(created.status_code, 201)
        document_id = created.json()['data']['document']['id']

        outsider_download = self.client.get(f'/api/v1/documents/{document_id}/download/', **auth_header(self.owner_b))
        self.assertEqual(outsider_download.status_code, 403)

        owner_download = self.client.get(f'/api/v1/documents/{document_id}/download/', **auth_header(self.owner_a))
        self.assertEqual(owner_download.status_code, 200)


class AIGenerationSecurityTests(TwoCompanyTestCase):
    def _request_plan(self, project, owner, prompt='Build a login flow', mentioned_user_ids=None):
        body = {'prompt': prompt}
        if mentioned_user_ids is not None:
            body['mentioned_user_ids'] = mentioned_user_ids
        return self.client.post(
            f"/api/v1/projects/{project['id']}/ai-plan/", json.dumps(body), content_type='application/json',
            **auth_header(owner),
        )

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_request_ai_plan_creates_pending_generation(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        response = self._request_plan(project, self.owner_a)
        self.assertEqual(response.status_code, 202)
        generation = response.json()['data']['generation']
        self.assertEqual(generation['status'], 'pending')
        self.assertIsNone(generation['saved_at'])
        self.assertEqual(generation['generated_tasks'], [])
        mock_delay.assert_called_once_with(generation['id'])

    def test_request_ai_plan_requires_a_prompt(self):
        project = self.create_project(owner=self.owner_a)
        response = self._request_plan(project, self.owner_a, prompt='')
        self.assertEqual(response.status_code, 422)

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_generation_hidden_from_other_company(self, mock_delay):
        project = self.create_project(owner=self.owner_a, visibility='private')
        created = self._request_plan(project, self.owner_a)
        generation_id = created.json()['data']['generation']['id']
        response = self.client.get(f'/api/v1/ai/generations/{generation_id}/', **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_second_plan_request_is_rejected_once_a_plan_is_saved(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        AIGeneration.objects.create(
            project_id=project['id'], requested_by=self.owner_a, status=AIGeneration.STATUS.COMPLETED,
            saved_at=timezone.now(),
        )
        response = self._request_plan(project, self.owner_a)
        self.assertEqual(response.status_code, 409)
        mock_delay.assert_not_called()

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_plan_request_accepts_an_eligible_assignee_pool_and_stores_it(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        response = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-plan/",
            json.dumps({'prompt': 'Build a login flow', 'assignee_ids': [str(self.member_a.id)], 'max_tasks': 5}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 202)
        generation = AIGeneration.objects.get(id=response.json()['data']['generation']['id'])
        self.assertEqual(generation.requested_assignee_ids, [str(self.member_a.id)])
        self.assertEqual(generation.max_tasks, 5)

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_plan_request_rejects_an_assignee_outside_the_company(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        response = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-plan/",
            json.dumps({'prompt': 'Build a login flow', 'assignee_ids': [str(self.owner_b.id)]}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)
        mock_delay.assert_not_called()

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_plan_request_rejects_max_tasks_out_of_bounds(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        response = self._request_plan(project, self.owner_a)
        self.assertEqual(response.status_code, 202)  # default max_tasks applies
        over_limit = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-plan/",
            json.dumps({'prompt': 'Build a login flow', 'max_tasks': 999}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(over_limit.status_code, 422)


class AIAssistantQuerySecurityTests(TwoCompanyTestCase):
    @patch('ai_agent.assistant_services.process_assistant_query.delay')
    def test_request_assistant_query_creates_pending_query(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        response = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-assistant/",
            json.dumps({'question': 'What tasks are To Do?'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 202)
        query = response.json()['data']['assistant_query']
        self.assertEqual(query['status'], 'pending')
        mock_delay.assert_called_once_with(query['id'])

    @patch('ai_agent.assistant_services.process_assistant_query.delay')
    def test_assistant_query_hidden_from_other_company(self, mock_delay):
        project = self.create_project(owner=self.owner_a, visibility='private')
        created = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-assistant/",
            json.dumps({'question': 'What tasks are To Do?'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        query_id = created.json()['data']['assistant_query']['id']
        response = self.client.get(f'/api/v1/ai/assistant-queries/{query_id}/', **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)

    def test_question_over_max_length_is_rejected(self):
        project = self.create_project(owner=self.owner_a)
        response = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-assistant/",
            json.dumps({'question': 'x' * 2001}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 422)

    def test_request_assistant_query_requires_authentication(self):
        project = self.create_project(owner=self.owner_a)
        response = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-assistant/",
            json.dumps({'question': 'What tasks are To Do?'}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)

    @patch('ai_agent.assistant_services.process_assistant_query.delay')
    def test_assistant_query_attaches_selected_pages_from_own_company(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        folder = PageFolder.objects.create(name='Docs', company=self.company_a, created_by=self.owner_a)
        page = Page.objects.create(folder=folder, title='Spec', created_by=self.owner_a)
        response = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-assistant/",
            json.dumps({'question': 'Summarize the spec.', 'page_ids': [str(page.id)]}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 202)
        pages = response.json()['data']['assistant_query']['pages']
        self.assertEqual([p['id'] for p in pages], [str(page.id)])

    @patch('ai_agent.assistant_services.process_assistant_query.delay')
    def test_assistant_query_rejects_a_page_from_another_company(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        other_folder = PageFolder.objects.create(name='B Docs', company=self.company_b, created_by=self.owner_b)
        other_page = Page.objects.create(folder=other_folder, title='Not yours', created_by=self.owner_b)
        response = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-assistant/",
            json.dumps({'question': 'Summarize this.', 'page_ids': [str(other_page.id)]}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)
        mock_delay.assert_not_called()

    @patch('ai_agent.assistant_services.process_assistant_query.delay')
    def test_delete_assistant_query_removes_it(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        created = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-assistant/",
            json.dumps({'question': 'What tasks are To Do?'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        query_id = created.json()['data']['assistant_query']['id']
        response = self.client.delete(f'/api/v1/ai/assistant-queries/{query_id}/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(f'/api/v1/ai/assistant-queries/{query_id}/', **auth_header(self.owner_a)).status_code, 404,
        )

    @patch('ai_agent.assistant_services.process_assistant_query.delay')
    def test_delete_assistant_query_hidden_from_other_company(self, mock_delay):
        project = self.create_project(owner=self.owner_a, visibility='private')
        created = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-assistant/",
            json.dumps({'question': 'What tasks are To Do?'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        query_id = created.json()['data']['assistant_query']['id']
        response = self.client.delete(f'/api/v1/ai/assistant-queries/{query_id}/', **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)

    @patch('ai_agent.assistant_services.process_assistant_query.delay')
    def test_delete_assistant_query_forbidden_for_non_requester_member(self, mock_delay):
        project = self.create_project(owner=self.owner_a, visibility='company')
        created = self.client.post(
            f"/api/v1/projects/{project['id']}/ai-assistant/",
            json.dumps({'question': 'What tasks are To Do?'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        query_id = created.json()['data']['assistant_query']['id']
        # member_a can view the query (company-visible project) but didn't
        # ask it and has no manage rights on the project -- must not delete it.
        response = self.client.delete(f'/api/v1/ai/assistant-queries/{query_id}/', **auth_header(self.member_a))
        self.assertEqual(response.status_code, 403)

    def test_save_as_page_splits_the_answer_into_structured_blocks(self):
        project = self.create_project(owner=self.owner_a)
        query = AIAssistantQuery.objects.create(
            project_id=project['id'], requested_by=self.owner_a, question='What is the plan?',
            status=AIAssistantQuery.STATUS.COMPLETED,
            answer='**Overview**\n\nSome context.\n\n- First point\n- Second point',
        )
        response = self.client.post(
            f'/api/v1/ai/assistant-queries/{query.id}/save-as-page/',
            json.dumps({'title': 'Saved answer', 'new_folder_name': 'AI Answers'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        page = Page.objects.get(id=response.json()['data']['page']['id'])
        self.assertEqual(page.blocks, [
            {'type': 'heading', 'text': 'Overview'},
            {'type': 'paragraph', 'text': 'Some context.'},
            {'type': 'list', 'items': ['First point', 'Second point']},
        ])


class AIHealthSummarySecurityTests(TwoCompanyTestCase):
    @patch('ai_agent.health_services.process_health_summary.delay')
    def test_request_health_summary_creates_pending_summary(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        response = self.client.post(f"/api/v1/projects/{project['id']}/ai-health-summary/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 202)
        summary = response.json()['data']['health_summary']
        self.assertEqual(summary['status'], 'pending')
        mock_delay.assert_called_once_with(summary['id'])

    @patch('ai_agent.health_services.process_health_summary.delay')
    def test_health_summary_hidden_from_other_company(self, mock_delay):
        project = self.create_project(owner=self.owner_a, visibility='private')
        created = self.client.post(f"/api/v1/projects/{project['id']}/ai-health-summary/", **auth_header(self.owner_a))
        summary_id = created.json()['data']['health_summary']['id']
        response = self.client.get(f'/api/v1/ai/health-summaries/{summary_id}/', **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)

    @patch('ai_agent.health_services.process_health_summary.delay')
    def test_rate_limit_boundary(self, mock_delay):
        """RATE_LIMIT_ENABLED is off globally (conftest.py) so the suite
        doesn't depend on a real Redis -- re-enabled locally for this one
        test, which does need a real Redis reachable at CELERY_BROKER_URL."""
        with self.settings(RATE_LIMIT_ENABLED=True):
            project = self.create_project(owner=self.owner_a)
            for _ in range(6):
                self.client.post(f"/api/v1/projects/{project['id']}/ai-health-summary/", **auth_header(self.owner_a))
            response = self.client.post(f"/api/v1/projects/{project['id']}/ai-health-summary/", **auth_header(self.owner_a))
            self.assertEqual(response.status_code, 429)

    def test_export_returns_an_xlsx_workbook_for_a_completed_summary(self):
        project = self.create_project(owner=self.owner_a)
        summary = AIProjectHealthSummary.objects.create(
            project_id=project['id'], requested_by=self.owner_a, status=AIProjectHealthSummary.STATUS.COMPLETED,
            summary='On track.', risk_level=AIProjectHealthSummary.RISK_LEVEL.LOW,
        )
        response = self.client.get(
            f"/api/v1/projects/{project['id']}/ai-health-summary/{summary.id}/export/", **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn('attachment', response['Content-Disposition'])

    def test_export_rejected_for_a_summary_that_isnt_completed(self):
        project = self.create_project(owner=self.owner_a)
        summary = AIProjectHealthSummary.objects.create(project_id=project['id'], requested_by=self.owner_a)
        response = self.client.get(
            f"/api/v1/projects/{project['id']}/ai-health-summary/{summary.id}/export/", **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_export_hidden_from_other_company(self):
        project = self.create_project(owner=self.owner_a)
        summary = AIProjectHealthSummary.objects.create(
            project_id=project['id'], requested_by=self.owner_a, status=AIProjectHealthSummary.STATUS.COMPLETED,
        )
        response = self.client.get(
            f"/api/v1/projects/{project['id']}/ai-health-summary/{summary.id}/export/", **auth_header(self.owner_b),
        )
        self.assertEqual(response.status_code, 403)


class NotificationTests(TwoCompanyTestCase):
    """Phase 9: notification isolation and the events that create one."""

    def test_user_cannot_see_another_users_notifications(self):
        Notification.objects.create(recipient=self.owner_a, type=Notification.Type.TASK_ASSIGNED, title='For A only')
        response = self.client.get('/api/v1/notifications/', **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['meta']['count'], 0)

    def test_user_cannot_mark_another_users_notification_as_read(self):
        notification = Notification.objects.create(
            recipient=self.owner_a, type=Notification.Type.TASK_ASSIGNED, title='For A only',
        )
        response = self.client.post(f'/api/v1/notifications/{notification.id}/read/', **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 404)
        notification.refresh_from_db()
        self.assertFalse(notification.is_read)

    def test_task_assignment_notifies_the_assignee(self):
        project = self.create_project(owner=self.owner_a)
        task = self.client.post(
            f"/api/v1/projects/{project['id']}/tasks/", json.dumps({'title': 'Build login page'}),
            content_type='application/json', **auth_header(self.owner_a),
        ).json()['data']['task']
        self.client.post(
            f"/api/v1/tasks/{task['id']}/assign/", json.dumps({'assigned_to_id': str(self.member_a.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertTrue(Notification.objects.filter(
            recipient=self.member_a, type=Notification.Type.TASK_ASSIGNED,
        ).exists())

    def test_task_completion_notifies_the_creator_but_not_the_assignee_completing_it(self):
        project = self.create_project(owner=self.owner_a)
        task = self.client.post(
            f"/api/v1/projects/{project['id']}/tasks/", json.dumps({'title': 'Build login page'}),
            content_type='application/json', **auth_header(self.owner_a),
        ).json()['data']['task']
        self.client.post(
            f"/api/v1/tasks/{task['id']}/assign/", json.dumps({'assigned_to_id': str(self.member_a.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.client.patch(
            f"/api/v1/tasks/{task['id']}/status/", json.dumps({'status': 'Done'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertTrue(Notification.objects.filter(
            recipient=self.owner_a, type=Notification.Type.TASK_COMPLETED,
        ).exists())
        self.assertFalse(Notification.objects.filter(
            recipient=self.member_a, type=Notification.Type.TASK_COMPLETED,
        ).exists())


class EligibleAssigneesTests(TwoCompanyTestCase):
    def test_falls_back_to_full_company_roster_when_project_has_no_department_or_team(self):
        project = self.create_project(owner=self.owner_a)
        response = self.client.get(f"/api/v1/projects/{project['id']}/eligible-assignees/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        ids = {row['id'] for row in response.json()['data']['results']}
        self.assertIn(str(self.member_a.id), ids)
        self.assertIn(str(self.owner_a.id), ids)
        self.assertNotIn(str(self.owner_b.id), ids)

    def test_scoped_to_the_projects_own_department_when_one_is_set(self):
        other_department = Department.objects.create(name='Sales', company=self.company_a)
        other_member = User.objects.create_user(email='sales@example.com', username='sales', password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(
            user=other_member, company=self.company_a, department=other_department,
            role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        project = self.create_project(owner=self.owner_a, department_id=str(self.department_a.id))
        response = self.client.get(f"/api/v1/projects/{project['id']}/eligible-assignees/", **auth_header(self.owner_a))
        ids = {row['id'] for row in response.json()['data']['results']}
        self.assertIn(str(self.member_a.id), ids)  # member_a is in department_a
        self.assertNotIn(str(other_member.id), ids)  # in Sales, not this project's department

    def test_scoped_to_the_projects_team_when_one_is_set_even_if_department_is_also_set(self):
        team = Team.objects.create(name='Launch Squad', company=self.company_a)
        team.members.set([self.member_a])
        project = self.create_project(
            owner=self.owner_a, department_id=str(self.department_a.id), team_id=str(team.id),
        )
        response = self.client.get(f"/api/v1/projects/{project['id']}/eligible-assignees/", **auth_header(self.owner_a))
        ids = {row['id'] for row in response.json()['data']['results']}
        self.assertEqual(ids, {str(self.member_a.id)})  # team takes precedence over department

    def test_outsider_cannot_view_eligible_assignees_for_a_private_project(self):
        project = self.create_project(owner=self.owner_a, visibility='private')
        response = self.client.get(f"/api/v1/projects/{project['id']}/eligible-assignees/", **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)

    def test_results_include_role_and_department_for_the_mention_and_assignee_picker(self):
        project = self.create_project(owner=self.owner_a, department_id=str(self.department_a.id))
        response = self.client.get(f"/api/v1/projects/{project['id']}/eligible-assignees/", **auth_header(self.owner_a))
        rows = {row['id']: row for row in response.json()['data']['results']}
        member_row = rows[str(self.member_a.id)]
        self.assertEqual(member_row['role'], CompanyUserProfile.Role.DEPARTMENT_MEMBER)
        self.assertEqual(member_row['department'], self.department_a.name)

        # The owner has no CompanyUserProfile row -- only reachable via the
        # unscoped company-roster fallback (no department/team on the project).
        unscoped_project = self.create_project(owner=self.owner_a)
        response = self.client.get(f"/api/v1/projects/{unscoped_project['id']}/eligible-assignees/", **auth_header(self.owner_a))
        owner_row = {row['id']: row for row in response.json()['data']['results']}[str(self.owner_a.id)]
        self.assertEqual(owner_row['role'], CompanyUserProfile.Role.Owner)
        self.assertIsNone(owner_row['department'])

    def test_open_task_count_is_scoped_to_this_project_and_excludes_done(self):
        project = self.create_project(owner=self.owner_a)
        other_project = self.create_project(owner=self.owner_a)
        Task.objects.create(project_id=project['id'], assigned_to=self.member_a, status=Task.STATUS.TODO)
        Task.objects.create(project_id=project['id'], assigned_to=self.member_a, status=Task.STATUS.IN_PROGRESS)
        Task.objects.create(project_id=project['id'], assigned_to=self.member_a, status=Task.STATUS.DONE)
        Task.objects.create(project_id=project['id'], assigned_to=self.member_a, status=Task.STATUS.TODO, is_deleted=True)
        # Open work on a DIFFERENT project must not leak into this count.
        Task.objects.create(project_id=other_project['id'], assigned_to=self.member_a, status=Task.STATUS.TODO)

        response = self.client.get(f"/api/v1/projects/{project['id']}/eligible-assignees/", **auth_header(self.owner_a))
        member_row = {row['id']: row for row in response.json()['data']['results']}[str(self.member_a.id)]
        self.assertEqual(member_row['open_task_count'], 2)


class AIPlanReviewSecurityTests(TwoCompanyTestCase):
    def _make_generation_with_draft(self, project_id, owner, **overrides):
        generation = AIGeneration.objects.create(
            project_id=project_id, requested_by=owner, status=AIGeneration.STATUS.COMPLETED,
        )
        draft = AIGeneratedTask.objects.create(
            generation=generation, temporary_id='t1', sequence=1, title='Define requirements', **overrides,
        )
        return generation, draft

    def test_comment_marks_the_task_unresolved(self):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(project['id'], self.owner_a)
        response = self.client.patch(
            f"/api/v1/ai/generations/{generation.id}/tasks/{draft.id}/comment/",
            json.dumps({'comment': 'Add more technical detail.'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.reviewer_comment, 'Add more technical detail.')
        self.assertFalse(draft.comment_resolved)

    def test_comment_hidden_from_other_company(self):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(project['id'], self.owner_a)
        response = self.client.patch(
            f"/api/v1/ai/generations/{generation.id}/tasks/{draft.id}/comment/",
            json.dumps({'comment': 'x'}), content_type='application/json',
            **auth_header(self.owner_b),
        )
        self.assertEqual(response.status_code, 403)

    def test_assign_rejects_a_user_outside_the_company(self):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(project['id'], self.owner_a)
        response = self.client.patch(
            f"/api/v1/ai/generations/{generation.id}/tasks/{draft.id}/assign/",
            json.dumps({'assigned_to_id': str(self.owner_b.id)}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_assign_accepts_an_eligible_member_and_clearing_it_again(self):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(project['id'], self.owner_a)
        set_response = self.client.patch(
            f"/api/v1/ai/generations/{generation.id}/tasks/{draft.id}/assign/",
            json.dumps({'assigned_to_id': str(self.member_a.id)}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(set_response.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.assigned_to_id, self.member_a.id)

        clear_response = self.client.patch(
            f"/api/v1/ai/generations/{generation.id}/tasks/{draft.id}/assign/",
            json.dumps({'assigned_to_id': None}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(clear_response.status_code, 200)
        draft.refresh_from_db()
        self.assertIsNone(draft.assigned_to_id)

    def test_regenerate_requires_at_least_one_unresolved_comment(self):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(project['id'], self.owner_a)
        response = self.client.post(
            f"/api/v1/ai/generations/{generation.id}/regenerate/", **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    @patch('api.routers.ai.process_ai_plan_regeneration.delay')
    def test_regenerate_enqueues_when_a_comment_is_pending(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(
            project['id'], self.owner_a, reviewer_comment='Needs detail.', comment_resolved=False,
        )
        response = self.client.post(
            f"/api/v1/ai/generations/{generation.id}/regenerate/", **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['data']['generation']['status'], 'processing')
        mock_delay.assert_called_once_with(str(generation.id))

    def test_regenerate_rejected_once_the_plan_is_saved(self):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(
            project['id'], self.owner_a, reviewer_comment='x', comment_resolved=False,
        )
        generation.saved_at = timezone.now()
        generation.save(update_fields=['saved_at'])
        response = self.client.post(
            f"/api/v1/ai/generations/{generation.id}/regenerate/", **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_save_persists_tasks_and_is_idempotent(self):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(project['id'], self.owner_a)
        first = self.client.post(f"/api/v1/ai/generations/{generation.id}/save/", **auth_header(self.owner_a))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.json()['data']['tasks']), 1)
        self.assertEqual(first.json()['data']['tasks'][0]['source'], 'ai_generated')

        generation.refresh_from_db()
        self.assertIsNotNone(generation.saved_at)

        second = self.client.post(f"/api/v1/ai/generations/{generation.id}/save/", **auth_header(self.owner_a))
        self.assertEqual(second.status_code, 409)

    def test_save_drops_an_assignee_that_became_ineligible_and_reports_it(self):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(project['id'], self.owner_a)
        # Assign to a member of company_a directly at the model layer, then
        # scope the project to a *different* department after the fact so
        # the assignment is no longer eligible by the time save runs.
        draft.assigned_to = self.member_a
        draft.save(update_fields=['assigned_to'])
        other_department = Department.objects.create(name='Sales', company=self.company_a)
        self.client.patch(
            f"/api/v1/projects/{project['id']}/", json.dumps({'department_id': str(other_department.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        response = self.client.post(f"/api/v1/ai/generations/{generation.id}/save/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        body = response.json()['data']
        self.assertEqual(body['invalid_assignee_temp_ids'], ['t1'])
        self.assertIsNone(body['tasks'][0]['assigned_to'])

    def test_save_hidden_from_other_company(self):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(project['id'], self.owner_a)
        response = self.client.post(f"/api/v1/ai/generations/{generation.id}/save/", **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)

    def test_save_applies_the_ai_suggested_assignee_when_no_human_override_exists(self):
        project = self.create_project(owner=self.owner_a)
        generation, draft = self._make_generation_with_draft(
            project['id'], self.owner_a, suggested_assignee=self.member_a,
        )
        response = self.client.post(f"/api/v1/ai/generations/{generation.id}/save/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['tasks'][0]['assigned_to'], str(self.member_a.id))

    def test_save_prefers_the_human_override_over_the_ai_suggestion(self):
        project = self.create_project(owner=self.owner_a)
        other_member = User.objects.create_user(email='other-a@example.com', username='other-a', password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(
            user=other_member, company=self.company_a, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        generation, draft = self._make_generation_with_draft(
            project['id'], self.owner_a, suggested_assignee=self.member_a, assigned_to=other_member,
        )
        response = self.client.post(f"/api/v1/ai/generations/{generation.id}/save/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['tasks'][0]['assigned_to'], str(other_member.id))


class AITaskContentRegenerationSecurityTests(TwoCompanyTestCase):
    def _create_ai_generated_task(self, project_id, owner):
        task = self.client.post(
            f'/api/v1/projects/{project_id}/tasks/', json.dumps({'title': 'Define requirements'}),
            content_type='application/json', **auth_header(owner),
        ).json()['data']['task']
        Task.objects.filter(id=task['id']).update(source=Task.SOURCE.AI_GENERATED)
        return task

    @patch('api.routers.ai.process_task_content_regeneration.delay')
    def test_regenerate_ai_content_enqueues_for_the_creator(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        task = self._create_ai_generated_task(project['id'], self.owner_a)
        response = self.client.post(
            f"/api/v1/tasks/{task['id']}/regenerate-ai-content/", json.dumps({'instructions': 'More detail.'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 202)
        mock_delay.assert_called_once()

    def test_regenerate_ai_content_rejects_a_manual_task(self):
        project = self.create_project(owner=self.owner_a)
        task = self.client.post(
            f"/api/v1/projects/{project['id']}/tasks/", json.dumps({'title': 'Manual task'}),
            content_type='application/json', **auth_header(self.owner_a),
        ).json()['data']['task']
        response = self.client.post(
            f"/api/v1/tasks/{task['id']}/regenerate-ai-content/", json.dumps({}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_regenerate_ai_content_hidden_from_other_company(self):
        project = self.create_project(owner=self.owner_a)
        task = self._create_ai_generated_task(project['id'], self.owner_a)
        response = self.client.post(
            f"/api/v1/tasks/{task['id']}/regenerate-ai-content/", json.dumps({}),
            content_type='application/json', **auth_header(self.owner_b),
        )
        self.assertEqual(response.status_code, 403)
