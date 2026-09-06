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
            f'/api/v1/company/members/{target.id}/role/', json.dumps({'role': role}),
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
            f'/api/v1/projects/{project.id}/', json.dumps({'title': 'Renamed'}),
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
            f'/api/v1/projects/{project.id}/', json.dumps({'title': 'Renamed'}),
            content_type='application/json', **auth_header(self.dl_a),
        )
        self.assertEqual(response.status_code, 403)

    def test_company_manager_can_create_a_department(self):
        response = self.client.post(
            '/api/v1/departments/', json.dumps({'name': 'Marketing'}),
            content_type='application/json', **auth_header(self.cm_a),
        )
        self.assertEqual(response.status_code, 201)

    def test_company_manager_can_send_an_invite(self):
        with patch('users.tasks.send_invitation_email'):
            response = self.client.post(
                '/api/v1/auth/send_invite/', json.dumps({'email': 'new-member@example.com'}),
                content_type='application/json', **auth_header(self.cm_a),
            )
        self.assertEqual(response.status_code, 200)

    # --- Owner-exclusive boundary ---------------------------------------

    def test_company_manager_cannot_start_a_checkout_for_the_company(self):
        plan = Plan.objects.create(name='Pro')
        response = self.client.post(
            '/api/v1/subscriptions/start-checkout/', json.dumps({'plan_id': str(plan.id)}),
            content_type='application/json', **auth_header(self.cm_a),
        )
        self.assertEqual(response.status_code, 400)

    def test_owner_can_invite_a_company_manager(self):
        with patch('users.tasks.send_invitation_email'):
            response = self.client.post(
                '/api/v1/auth/send_invite/', json.dumps({'email': 'new-cm@example.com', 'role': 'CM'}),
                content_type='application/json', **auth_header(self.owner_a),
            )
        self.assertEqual(response.status_code, 200)

    def test_company_manager_cannot_invite_another_company_manager(self):
        with patch('users.tasks.send_invitation_email'):
            response = self.client.post(
                '/api/v1/auth/send_invite/', json.dumps({'email': 'new-cm@example.com', 'role': 'CM'}),
                content_type='application/json', **auth_header(self.cm_a),
            )
        self.assertEqual(response.status_code, 403)

    def test_department_leader_cannot_invite_a_company_manager(self):
        with patch('users.tasks.send_invitation_email'):
            response = self.client.post(
                '/api/v1/auth/send_invite/', json.dumps({'email': 'new-cm@example.com', 'role': 'CM'}),
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
            f'/api/v1/company/members/{self.member_a.id}/role/', json.dumps({'role': 'DL'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)


class MemberLifecycleTests(TwoCompanyTestCase):
    """Activate/deactivate, department change, and removal (with the
    ownership-reassignment it requires). CompanyUserProfile.is_active is
    company-scoped -- deactivation must never touch the global auth flag."""

    def setUp(self):
        super().setUp()
        self.cm_a = self._make_profile('cm-a', CompanyUserProfile.Role.COMPANY_MANAGER)

    def _make_profile(self, slug, role, department=None):
        user = User.objects.create_user(email=f'{slug}@example.com', username=slug, password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(user=user, company=self.company_a, role=role, department=department)
        return user

    def set_status(self, target, is_active, requester=None):
        return self.client.patch(
            f'/api/v1/company/members/{target.id}/status/', json.dumps({'is_active': is_active}),
            content_type='application/json', **auth_header(requester or self.owner_a),
        )

    def remove(self, target, reassign_to=None, requester=None):
        body = {'reassign_to_user_id': str(reassign_to.id)} if reassign_to else {}
        return self.client.post(
            f'/api/v1/company/members/{target.id}/remove/', json.dumps(body),
            content_type='application/json', **auth_header(requester or self.owner_a),
        )

    # --- Activate / deactivate ------------------------------------------

    def test_owner_can_deactivate_a_member(self):
        response = self.set_status(self.member_a, False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a).is_active)

    def test_deactivated_members_company_scoped_calls_are_rejected_but_login_still_works(self):
        self.set_status(self.member_a, False)
        response = self.client.get('/api/v1/projects/', **auth_header(self.member_a))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['meta']['count'], 0)
        # Global Django auth is untouched -- the account itself still logs in.
        self.member_a.refresh_from_db()
        self.assertTrue(self.member_a.is_active)

    def test_reactivating_a_deactivated_member_restores_access(self):
        self.set_status(self.member_a, False)
        response = self.set_status(self.member_a, True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a).is_active)

    def test_department_member_cannot_deactivate_a_peer(self):
        peer = self._make_profile('peer-a', CompanyUserProfile.Role.DEPARTMENT_MEMBER)
        response = self.set_status(peer, False, requester=self.member_a)
        self.assertEqual(response.status_code, 403)

    def test_cannot_deactivate_self(self):
        response = self.set_status(self.cm_a, False, requester=self.cm_a)
        self.assertEqual(response.status_code, 400)

    def test_cannot_deactivate_the_owner(self):
        response = self.set_status(self.owner_a, False, requester=self.cm_a)
        self.assertEqual(response.status_code, 404)

    def test_status_change_is_scoped_to_the_callers_own_company(self):
        response = self.set_status(self.member_a, False, requester=self.owner_b)
        self.assertEqual(response.status_code, 404)

    # --- Department change ------------------------------------------------

    def test_owner_can_change_a_members_department(self):
        other_department = Department.objects.create(name='Sales', company=self.company_a)
        response = self.client.patch(
            f'/api/v1/company/members/{self.member_a.id}/department/',
            json.dumps({'department_id': str(other_department.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a).department_id,
            other_department.id,
        )

    def test_department_from_another_company_is_rejected(self):
        other_company_department = Department.objects.create(name='Sales', company=self.company_b)
        response = self.client.patch(
            f'/api/v1/company/members/{self.member_a.id}/department/',
            json.dumps({'department_id': str(other_company_department.id)}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)

    # --- Removal + ownership reassignment ---------------------------------

    def test_removing_a_member_with_no_active_work_succeeds_immediately(self):
        response = self.remove(self.member_a)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CompanyUserProfile.objects.filter(user=self.member_a, company=self.company_a).exists())

    def test_removing_an_active_project_owner_requires_reassignment(self):
        project = Project.objects.create(
            title='Owned by member', company=self.company_a, created_by=self.member_a, current_owner=self.member_a,
        )
        response = self.remove(self.member_a)
        self.assertEqual(response.status_code, 409)
        body = response.json()['data']
        self.assertEqual(body['projects'], [{'id': str(project.id), 'title': 'Owned by member'}])
        # Removal did not happen.
        self.assertTrue(CompanyUserProfile.objects.filter(user=self.member_a, company=self.company_a).exists())

    def test_removing_with_a_valid_reassignee_transfers_ownership_and_preserves_history(self):
        project = Project.objects.create(
            title='Owned by member', company=self.company_a, created_by=self.member_a, current_owner=self.member_a,
        )
        response = self.remove(self.member_a, reassign_to=self.cm_a)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['reassigned_count'], 1)
        project.refresh_from_db()
        self.assertEqual(project.current_owner_id, self.cm_a.id)
        # created_by (historical) is never overwritten by a removal reassignment.
        self.assertEqual(project.created_by_id, self.member_a.id)
        self.assertFalse(CompanyUserProfile.objects.filter(user=self.member_a, company=self.company_a).exists())

    def test_reassignee_from_another_company_is_rejected(self):
        Project.objects.create(
            title='Owned by member', company=self.company_a, created_by=self.member_a, current_owner=self.member_a,
        )
        response = self.remove(self.member_a, reassign_to=self.owner_b)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(CompanyUserProfile.objects.filter(user=self.member_a, company=self.company_a).exists())

    def test_removing_a_department_leader_clears_leadership(self):
        dl = self._make_profile('dl-remove', CompanyUserProfile.Role.DEPARTMENT_LEADER, department=self.department_a)
        self.department_a.leader = dl
        self.department_a.save(update_fields=['leader'])
        response = self.remove(dl)
        self.assertEqual(response.status_code, 200)
        self.department_a.refresh_from_db()
        self.assertIsNone(self.department_a.leader_id)

    def test_department_leader_cannot_remove_a_member(self):
        dl = self._make_profile('dl-a', CompanyUserProfile.Role.DEPARTMENT_LEADER)
        response = self.remove(self.member_a, requester=dl)
        self.assertEqual(response.status_code, 403)

    def test_company_manager_cannot_remove_another_company_manager(self):
        other_cm = self._make_profile('cm-a-2', CompanyUserProfile.Role.COMPANY_MANAGER)
        response = self.remove(other_cm, requester=self.cm_a)
        self.assertEqual(response.status_code, 403)

    def test_owner_can_remove_a_company_manager(self):
        response = self.remove(self.cm_a)
        self.assertEqual(response.status_code, 200)

    def test_cannot_remove_self(self):
        response = self.remove(self.cm_a, requester=self.cm_a)
        self.assertEqual(response.status_code, 400)

    def test_cannot_remove_the_owner(self):
        response = self.remove(self.owner_a, requester=self.cm_a)
        self.assertEqual(response.status_code, 404)

    def test_removal_is_scoped_to_the_callers_own_company(self):
        response = self.remove(self.member_a, requester=self.owner_b)
        self.assertEqual(response.status_code, 404)

    def test_requires_authentication(self):
        response = self.client.post(f'/api/v1/company/members/{self.member_a.id}/remove/', content_type='application/json')
        self.assertEqual(response.status_code, 401)


class MemberDetailTests(TwoCompanyTestCase):
    def get_detail(self, target, requester=None):
        return self.client.get(f'/api/v1/company/members/{target.id}/', **auth_header(requester or self.owner_a))

    def test_owner_can_view_a_members_detail(self):
        response = self.get_detail(self.member_a)
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']['member']
        self.assertEqual(data['role'], CompanyUserProfile.Role.DEPARTMENT_MEMBER)
        self.assertEqual(data['department_name'], self.department_a.name)
        self.assertIn('workload', data)
        self.assertIn('profession', data)

    def test_owner_detail_has_no_department_and_owner_role(self):
        response = self.get_detail(self.owner_a)
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']['member']
        self.assertEqual(data['role'], CompanyUserProfile.Role.Owner)
        self.assertIsNone(data['department_name'])
        self.assertIsNone(data['profession'])

    def test_outsider_cannot_view_member_detail(self):
        outsider = User.objects.create_user(email='outsider@example.com', username='outsider', password='Kx9#mQ2vLp8Z')
        response = self.get_detail(self.member_a, requester=outsider)
        self.assertEqual(response.status_code, 403)

    def test_detail_is_scoped_to_the_callers_own_company(self):
        response = self.get_detail(self.member_a, requester=self.owner_b)
        self.assertEqual(response.status_code, 404)

    def test_requires_authentication(self):
        response = self.client.get(f'/api/v1/company/members/{self.member_a.id}/')
        self.assertEqual(response.status_code, 401)


class ThemePreferenceTests(TwoCompanyTestCase):
    def update_theme(self, user, theme):
        return self.client.patch(
            '/api/v1/company/members/me/theme/', json.dumps({'theme': theme}),
            content_type='application/json', **auth_header(user),
        )

    def test_owner_can_update_theme(self):
        response = self.update_theme(self.owner_a, 'dark')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['theme'], 'dark')
        self.owner_a.refresh_from_db()
        self.assertEqual(self.owner_a.theme, 'dark')

    def test_invalid_theme_is_rejected(self):
        # 'theme' is a Literal['light', 'dark', 'system'] at the schema layer
        # (same pattern as MemberRoleIn.role) -- Ninja's own schema
        # validation rejects an out-of-set value before update_user_theme's
        # own defensive check would ever run.
        response = self.update_theme(self.owner_a, 'purple')
        self.assertEqual(response.status_code, 422)

    def test_requires_authentication(self):
        response = self.client.patch(
            '/api/v1/company/members/me/theme/', json.dumps({'theme': 'dark'}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)
