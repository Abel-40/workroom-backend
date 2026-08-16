"""Security-critical tests for the Django Ninja API.

Phase 0 baseline: prove the authorization bugs fixed in this phase stay
fixed, per DEVELOPMENT_RULES Rule 12 (test security paths before raw
coverage). Phases 1-5 add project/task/document/AI-generation coverage,
focused on cross-tenant rejection over raw endpoint coverage.
"""

import hashlib
import json
from unittest.mock import patch

from company.models import Company, Sector
from departments_and_teams.models import Department
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
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
        self.owner_a = User.objects.create_user(email='owner-a@example.com', username='owner-a', password='Kx9#mQ2vLp8Z')
        self.company_a = Company.objects.create(name='Company A', owner=self.owner_a, sector=sector)
        self.department_a = Department.objects.create(name='Engineering', company=self.company_a)
        self.member_a = User.objects.create_user(email='member-a@example.com', username='member-a', password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(
            user=self.member_a, company=self.company_a, department=self.department_a,
            role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )

        self.owner_b = User.objects.create_user(email='owner-b@example.com', username='owner-b', password='Kx9#mQ2vLp8Z')
        self.company_b = Company.objects.create(name='Company B', owner=self.owner_b, sector=sector)

    def create_project(self, owner=None, **overrides):
        body = {'title': 'Website Revamp', 'visibility': 'company'}
        body.update(overrides)
        response = self.client.post(
            '/api/projects/', json.dumps(body), content_type='application/json',
            **auth_header(owner or self.owner_a),
        )
        return response.json()['data']['project']


class CompanyRegistrationSecurityTests(TestCase):
    def setUp(self):
        self.sector = Sector.objects.create(name='Software')
        self.user = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')

    def test_register_company_requires_authentication(self):
        response = self.client.post(
            '/api/company/register/', {'name': 'Acme', 'sector': self.sector.id}, content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)

    def test_register_company_ignores_client_supplied_owner(self):
        """A client-supplied 'owner' must never override the authenticated user."""
        victim = User.objects.create_user(email='victim@example.com', username='victim', password='Kx9#mQ2vLp8Z')
        response = self.client.post(
            '/api/company/register/',
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
            '/api/company/register/', {'name': 'Second', 'sector': self.sector.id},
            content_type='application/json', **auth_header(self.user),
        )
        self.assertEqual(response.status_code, 400)


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
        self.outsider = User.objects.create_user(email='outsider@example.com', username='outsider', password='Kx9#mQ2vLp8Z')

    def test_invite_cannot_grant_owner_role(self):
        """A company must never gain a second Owner-role profile via invitation."""
        response = self.client.post(
            '/api/auth/send_invite/', {'email': 'new@example.com', 'role': 'Owner'},
            content_type='application/json', **auth_header(self.owner),
        )
        self.assertEqual(response.status_code, 422)

    def test_default_invite_role_is_department_member(self):
        response = self.client.post(
            '/api/auth/send_invite/', {'email': 'new@example.com'},
            content_type='application/json', **auth_header(self.owner),
        )
        self.assertEqual(response.status_code, 200)

    def test_user_without_company_cannot_send_invite(self):
        response = self.client.post(
            '/api/auth/send_invite/', {'email': 'new@example.com'},
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

    def send_invite_and_get_raw_token(self, email='invitee@example.com'):
        with patch('api.api.send_invitation_email') as mock_send:
            self.client.post(
                '/api/auth/send_invite/', {'email': email},
                content_type='application/json', **auth_header(self.owner),
            )
        raw_token = mock_send.call_args.args[-1].split('token=')[-1]
        return raw_token

    def test_raw_token_is_never_stored_in_the_database(self):
        raw_token = self.send_invite_and_get_raw_token()
        invite = PendingInvite.objects.get(email='invitee@example.com')
        self.assertNotEqual(invite.token_hash, raw_token)
        self.assertEqual(invite.token_hash, hashlib.sha256(raw_token.encode()).hexdigest())

    def test_accept_invite_rejects_wrong_token(self):
        self.send_invite_and_get_raw_token()
        response = self.client.post(
            '/api/emp/accept_invite/',
            {'token': 'not-the-real-token', 'password': 'Kx9#mQ2vLp8Z', 'username': 'invitee'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_accept_invite_deletes_the_pending_invite_row(self):
        raw_token = self.send_invite_and_get_raw_token()
        self.assertEqual(PendingInvite.objects.filter(email='invitee@example.com').count(), 1)
        response = self.client.post(
            '/api/emp/accept_invite/',
            {'token': raw_token, 'password': 'Kx9#mQ2vLp8Z', 'username': 'invitee'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(PendingInvite.objects.filter(email='invitee@example.com').count(), 0)

    def test_send_invite_sets_email_sent_true_on_success(self):
        self.send_invite_and_get_raw_token()
        invite = PendingInvite.objects.get(email='invitee@example.com')
        self.assertTrue(invite.email_sent)

    def test_send_invite_marks_email_sent_false_on_delivery_failure(self):
        with patch('api.api.send_invitation_email', side_effect=RuntimeError('smtp down')):
            response = self.client.post(
                '/api/auth/send_invite/', {'email': 'invitee@example.com'},
                content_type='application/json', **auth_header(self.owner),
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['data']['email_sent'])
        invite = PendingInvite.objects.get(email='invitee@example.com')
        self.assertFalse(invite.email_sent)


class SignupPasswordValidationTests(TestCase):
    def test_signup_rejects_weak_password(self):
        response = self.client.post(
            '/api/auth/signup/',
            {'email': 'weak@example.com', 'username': 'weak', 'password': 'password'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_signup_accepts_strong_password(self):
        response = self.client.post(
            '/api/auth/signup/',
            {'email': 'strong@example.com', 'username': 'strong', 'password': 'Kx9#mQ2vLp8Z'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)


class ProjectSecurityTests(TwoCompanyTestCase):
    def test_create_project_requires_company_membership(self):
        outsider = User.objects.create_user(email='outsider@example.com', username='outsider', password='Kx9#mQ2vLp8Z')
        response = self.client.post(
            '/api/projects/', json.dumps({'title': 'Ghost Project'}), content_type='application/json',
            **auth_header(outsider),
        )
        self.assertEqual(response.status_code, 400)

    def test_created_project_is_scoped_to_callers_own_company(self):
        project = self.create_project(owner=self.owner_a)
        self.assertEqual(project['company_id'], str(self.company_a.id))

    def test_company_project_hidden_from_other_company(self):
        project = self.create_project(owner=self.owner_a, visibility='company')
        response = self.client.get(f"/api/projects/{project['id']}/", **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)

    def test_company_project_listed_only_for_own_company(self):
        self.create_project(owner=self.owner_a, visibility='company')
        response = self.client.get('/api/projects/', **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['meta']['count'], 0)

    def test_private_project_hidden_from_non_collaborator_company_member(self):
        project = self.create_project(owner=self.owner_a, visibility='private')
        response = self.client.get(f"/api/projects/{project['id']}/", **auth_header(self.member_a))
        self.assertEqual(response.status_code, 403)

    def test_public_project_visible_across_companies(self):
        project = self.create_project(owner=self.owner_a, visibility='public')
        response = self.client.get(f"/api/projects/{project['id']}/", **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 200)

    def test_only_manager_can_update_project(self):
        project = self.create_project(owner=self.owner_a)
        response = self.client.patch(
            f"/api/projects/{project['id']}/", json.dumps({'title': 'Hijacked'}), content_type='application/json',
            **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 403)

    def test_client_supplied_company_field_is_ignored(self):
        """ProjectIn has no company field at all; sending one must have zero effect."""
        response = self.client.post(
            '/api/projects/',
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
            f'/api/projects/{project_id}/tasks/', json.dumps(body), content_type='application/json',
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
            f"/api/tasks/{task['id']}/assign/",
            json.dumps({'assigned_to_id': str(self.owner_b.id)}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_task_assignment_accepts_eligible_company_member(self):
        project = self.create_project(owner=self.owner_a)
        task = self.create_task(project['id']).json()['data']['task']
        response = self.client.post(
            f"/api/tasks/{task['id']}/assign/",
            json.dumps({'assigned_to_id': str(self.member_a.id)}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)

    def test_assignee_can_update_status_but_outsider_cannot(self):
        project = self.create_project(owner=self.owner_a)
        task = self.create_task(project['id']).json()['data']['task']
        self.client.post(
            f"/api/tasks/{task['id']}/assign/",
            json.dumps({'assigned_to_id': str(self.member_a.id)}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        ok = self.client.patch(
            f"/api/tasks/{task['id']}/status/", json.dumps({'status': 'In Progress'}), content_type='application/json',
            **auth_header(self.member_a),
        )
        self.assertEqual(ok.status_code, 200)
        forbidden = self.client.patch(
            f"/api/tasks/{task['id']}/status/", json.dumps({'status': 'Done'}), content_type='application/json',
            **auth_header(self.owner_b),
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_invalid_status_value_rejected(self):
        project = self.create_project(owner=self.owner_a)
        task = self.create_task(project['id']).json()['data']['task']
        response = self.client.patch(
            f"/api/tasks/{task['id']}/status/", json.dumps({'status': 'Cancelled'}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 422)


class DocumentSecurityTests(TwoCompanyTestCase):
    def test_disallowed_content_type_rejected(self):
        project = self.create_project(owner=self.owner_a)
        upload = SimpleUploadedFile('payload.exe', b'MZ...', content_type='application/x-msdownload')
        response = self.client.post(
            f"/api/projects/{project['id']}/documents/", {'file': upload, 'label': 'binary'},
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_valid_document_upload_and_cross_tenant_download_rejected(self):
        project = self.create_project(owner=self.owner_a)
        upload = SimpleUploadedFile('spec.txt', b'project spec', content_type='text/plain')
        created = self.client.post(
            f"/api/projects/{project['id']}/documents/", {'file': upload, 'label': 'spec'},
            **auth_header(self.owner_a),
        )
        self.assertEqual(created.status_code, 201)
        document_id = created.json()['data']['document']['id']

        outsider_download = self.client.get(f'/api/documents/{document_id}/download/', **auth_header(self.owner_b))
        self.assertEqual(outsider_download.status_code, 403)

        owner_download = self.client.get(f'/api/documents/{document_id}/download/', **auth_header(self.owner_a))
        self.assertEqual(owner_download.status_code, 200)


class AIGenerationSecurityTests(TwoCompanyTestCase):
    @patch('ai_agent.services.process_ai_generation.delay')
    def test_request_ai_plan_creates_pending_generation(self, mock_delay):
        project = self.create_project(owner=self.owner_a)
        response = self.client.post(f"/api/projects/{project['id']}/ai-plan/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 202)
        generation = response.json()['data']['generation']
        self.assertEqual(generation['status'], 'pending')
        mock_delay.assert_called_once_with(generation['id'])

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_generation_hidden_from_other_company(self, mock_delay):
        project = self.create_project(owner=self.owner_a, visibility='private')
        created = self.client.post(f"/api/projects/{project['id']}/ai-plan/", **auth_header(self.owner_a))
        generation_id = created.json()['data']['generation']['id']
        response = self.client.get(f'/api/ai/generations/{generation_id}/', **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)
