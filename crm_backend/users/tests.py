"""Company Manager role: operational parity with Owner (projects regardless
of department, departments/teams/invites), the Owner-exclusive boundary
(billing, minting/revoking a peer Company Manager), and the new
PATCH /company/members/{id}/role/ endpoint's privilege-escalation guards.
Reuses TwoCompanyTestCase (api/tests.py) for cross-tenant coverage the same
way every other endpoint's tests do.
"""

import json
from unittest.mock import patch

from api.tests import TwoCompanyTestCase, auth_header
from departments_and_teams.models import Department, Team
from plans.models import Plan
from projects_and_tasks.models import Project

from users.models import CompanyUserProfile, User


class CompanyManagerRoleTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.cm_a = self._make_profile('cm-a', CompanyUserProfile.Role.COMPANY_MANAGER)
        self.dl_a = self._make_profile(
            'dl-a', CompanyUserProfile.Role.DEPARTMENT_LEADER, department=self.department_a,
        )

    def _make_profile(self, slug, role, department=None):
        user = User.objects.create_user(email=f'{slug}@example.com', username=slug, password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(user=user, company=self.company_a, role=role, department=department)
        return user

    def change_role(self, requester, target, role):
        return self.client.patch(
            f'/api/company/members/{target.id}/role/', json.dumps({'role': role}),
            content_type='application/json', **auth_header(requester),
        )

    # --- Operational parity with Owner --------------------------------

    def test_company_manager_can_manage_a_project_outside_their_own_department(self):
        other_department = Department.objects.create(name='Sales', company=self.company_a)
        project = Project.objects.create(
            title='Outside CM department', company=self.company_a, created_by=self.owner_a,
            department=other_department,
        )
        response = self.client.patch(
            f'/api/projects/{project.id}/', json.dumps({'title': 'Renamed'}),
            content_type='application/json', **auth_header(self.cm_a),
        )
        self.assertEqual(response.status_code, 200)

    def test_department_leader_cannot_manage_a_project_outside_their_own_department(self):
        other_department = Department.objects.create(name='Sales', company=self.company_a)
        project = Project.objects.create(
            title='Outside DL department', company=self.company_a, created_by=self.owner_a,
            department=other_department,
        )
        response = self.client.patch(
            f'/api/projects/{project.id}/', json.dumps({'title': 'Renamed'}),
            content_type='application/json', **auth_header(self.dl_a),
        )
        self.assertEqual(response.status_code, 403)

    def test_company_manager_can_create_a_department(self):
        response = self.client.post(
            '/api/departments/', json.dumps({'name': 'Marketing'}),
            content_type='application/json', **auth_header(self.cm_a),
        )
        self.assertEqual(response.status_code, 201)

    def test_company_manager_can_send_an_invite(self):
        with patch('users.tasks.send_invitation_email'):
            response = self.client.post(
                '/api/auth/send_invite/', json.dumps({'email': 'new-member@example.com'}),
                content_type='application/json', **auth_header(self.cm_a),
            )
        self.assertEqual(response.status_code, 200)

    # --- Owner-exclusive boundary ---------------------------------------

    def test_company_manager_cannot_start_a_checkout_for_the_company(self):
        plan = Plan.objects.create(name='Pro')
        response = self.client.post(
            '/api/subscriptions/start-checkout/', json.dumps({'plan_id': str(plan.id)}),
            content_type='application/json', **auth_header(self.cm_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_owner_can_invite_a_company_manager(self):
        with patch('users.tasks.send_invitation_email'):
            response = self.client.post(
                '/api/auth/send_invite/', json.dumps({'email': 'new-cm@example.com', 'role': 'CM'}),
                content_type='application/json', **auth_header(self.owner_a),
            )
        self.assertEqual(response.status_code, 200)

    def test_company_manager_cannot_invite_another_company_manager(self):
        with patch('users.tasks.send_invitation_email'):
            response = self.client.post(
                '/api/auth/send_invite/', json.dumps({'email': 'new-cm@example.com', 'role': 'CM'}),
                content_type='application/json', **auth_header(self.cm_a),
            )
        self.assertEqual(response.status_code, 403)

    def test_department_leader_cannot_invite_a_company_manager(self):
        with patch('users.tasks.send_invitation_email'):
            response = self.client.post(
                '/api/auth/send_invite/', json.dumps({'email': 'new-cm@example.com', 'role': 'CM'}),
                content_type='application/json', **auth_header(self.dl_a),
            )
        self.assertEqual(response.status_code, 403)

    # --- PATCH /company/members/{id}/role/ ------------------------------

    def test_owner_can_promote_a_member_to_company_manager(self):
        response = self.change_role(self.owner_a, self.member_a, 'CM')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a).role,
            CompanyUserProfile.Role.COMPANY_MANAGER,
        )

    def test_company_manager_cannot_promote_another_member_to_company_manager(self):
        response = self.change_role(self.cm_a, self.member_a, 'CM')
        self.assertEqual(response.status_code, 403)

    def test_company_manager_cannot_demote_another_company_manager(self):
        other_cm = self._make_profile('cm-a-2', CompanyUserProfile.Role.COMPANY_MANAGER)
        response = self.change_role(self.cm_a, other_cm, 'DM')
        self.assertEqual(response.status_code, 403)

    def test_company_manager_can_demote_a_department_leader_and_clears_leadership(self):
        self.department_a.leader = self.dl_a
        self.department_a.save(update_fields=['leader'])
        team = Team.objects.create(name='Core', company=self.company_a, leader=self.dl_a)

        response = self.change_role(self.cm_a, self.dl_a, 'DM')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            CompanyUserProfile.objects.get(user=self.dl_a, company=self.company_a).role,
            CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        self.department_a.refresh_from_db()
        self.assertIsNone(self.department_a.leader_id)
        team.refresh_from_db()
        self.assertIsNone(team.leader_id)

    def test_department_leader_cannot_change_anyones_role(self):
        response = self.change_role(self.dl_a, self.member_a, 'DL')
        self.assertEqual(response.status_code, 403)

    def test_department_member_cannot_change_anyones_role(self):
        response = self.change_role(self.member_a, self.dl_a, 'DM')
        self.assertEqual(response.status_code, 403)

    def test_requester_cannot_change_their_own_role(self):
        response = self.change_role(self.cm_a, self.cm_a, 'DL')
        self.assertEqual(response.status_code, 400)

    def test_owner_cannot_be_a_role_change_target(self):
        response = self.change_role(self.cm_a, self.owner_a, 'DM')
        self.assertEqual(response.status_code, 404)

    def test_role_change_is_scoped_to_the_caller_own_company(self):
        response = self.change_role(self.owner_a, self.owner_b, 'DM')
        self.assertEqual(response.status_code, 404)

    def test_role_change_is_idempotent(self):
        response = self.change_role(self.owner_a, self.dl_a, 'DL')
        self.assertEqual(response.status_code, 200)

    def test_requires_authentication(self):
        response = self.client.patch(
            f'/api/company/members/{self.member_a.id}/role/', json.dumps({'role': 'DL'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)
