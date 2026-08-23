"""Department/team directory API: happy path, membership requirement, and
cross-tenant rejection, reusing the TwoCompanyTestCase fixture (api/tests.py)
so isolation is checked the same way every other endpoint's tests check it.
"""

import json

from api.tests import TwoCompanyTestCase, auth_header
from users.models import CompanyUserProfile, User

from departments_and_teams.models import DefaultDepartment, Department, Team


class DepartmentListTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        Department.objects.create(name='Design', company=self.company_a, leader=self.owner_a)

    def list_departments(self, user):
        return self.client.get('/api/departments/', **auth_header(user))

    def test_owner_lists_their_company_departments(self):
        response = self.list_departments(self.owner_a)
        self.assertEqual(response.status_code, 200)
        names = {item['name'] for item in response.json()['data']['results']}
        self.assertEqual(names, {'Engineering', 'Design'})

    def test_member_can_list_departments(self):
        response = self.list_departments(self.member_a)
        self.assertEqual(response.status_code, 200)

    def test_departments_are_scoped_to_the_caller_own_company(self):
        response = self.list_departments(self.owner_b)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['results'], [])

    def test_requires_authentication(self):
        response = self.client.get('/api/departments/')
        self.assertEqual(response.status_code, 401)

    def test_list_reports_member_count(self):
        response = self.list_departments(self.owner_a)
        by_name = {item['name']: item for item in response.json()['data']['results']}
        # member_a belongs to department_a (see TwoCompanyTestCase.setUp)
        self.assertEqual(by_name['Engineering']['member_count'], 1)
        self.assertEqual(by_name['Design']['member_count'], 0)


class DepartmentCreateTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.dl_a = self._make_profile('dl-a', CompanyUserProfile.Role.DEPARTMENT_LEADER, self.department_a)
        self.cm_a = self._make_profile('cm-a', CompanyUserProfile.Role.COMPANY_MANAGER)

    def _make_profile(self, slug, role, department=None):
        from users.models import User

        user = User.objects.create_user(email=f'{slug}@example.com', username=slug, password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(user=user, company=self.company_a, department=department, role=role)
        return user

    def create_department(self, user, **overrides):
        body = {'name': 'Marketing'}
        body.update(overrides)
        return self.client.post(
            '/api/departments/', json.dumps(body), content_type='application/json', **auth_header(user),
        )

    def test_owner_can_create_department(self):
        response = self.create_department(self.owner_a, description='Growth & campaigns')
        self.assertEqual(response.status_code, 201)
        data = response.json()['data']['department']
        self.assertEqual(data['name'], 'Marketing')
        self.assertEqual(data['description'], 'Growth & campaigns')
        self.assertTrue(Department.objects.filter(company=self.company_a, name='Marketing').exists())

    def test_department_leader_can_create_department(self):
        response = self.create_department(self.dl_a)
        self.assertEqual(response.status_code, 201)

    def test_company_manager_can_create_department(self):
        response = self.create_department(self.cm_a)
        self.assertEqual(response.status_code, 201)

    def test_department_member_cannot_create_department(self):
        response = self.create_department(self.member_a)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Department.objects.filter(company=self.company_a, name='Marketing').exists())

    def test_duplicate_name_within_company_is_rejected(self):
        response = self.create_department(self.owner_a, name='Engineering')
        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.json()['errors'])

    def test_leader_from_another_company_is_rejected(self):
        response = self.create_department(self.owner_a, leader_id=str(self.owner_b.id))
        self.assertEqual(response.status_code, 400)
        self.assertIn('leader_id', response.json()['errors'])

    def test_new_department_is_scoped_to_the_creator_own_company(self):
        self.create_department(self.owner_a)
        response = self.create_department(self.owner_b)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Department.objects.filter(name='Marketing').count(), 2)
        self.assertTrue(Department.objects.filter(name='Marketing', company=self.company_a).exists())
        self.assertTrue(Department.objects.filter(name='Marketing', company=self.company_b).exists())

    def test_requires_authentication(self):
        response = self.client.post(
            '/api/departments/', json.dumps({'name': 'Marketing'}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)


class DepartmentUpdateTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.dl_a = self._make_profile('dl-a', CompanyUserProfile.Role.DEPARTMENT_LEADER, self.department_a)
        self.dm_a = self._make_profile('dm-plain', CompanyUserProfile.Role.DEPARTMENT_MEMBER)
        self.other_department = Department.objects.create(name='Design', company=self.company_a)

    def _make_profile(self, slug, role, department=None):
        user = User.objects.create_user(email=f'{slug}@example.com', username=slug, password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(user=user, company=self.company_a, department=department, role=role)
        return user

    def patch_department(self, department_id, body, user=None):
        return self.client.patch(
            f'/api/departments/{department_id}/', json.dumps(body), content_type='application/json',
            **auth_header(user or self.owner_a),
        )

    def test_owner_can_rename_and_redescribe(self):
        response = self.patch_department(
            self.department_a.id, {'name': 'Product Engineering', 'description': 'Renamed'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']['department']
        self.assertEqual(data['name'], 'Product Engineering')
        self.assertEqual(data['description'], 'Renamed')

    def test_department_member_cannot_update(self):
        response = self.patch_department(self.department_a.id, {'description': 'Hijacked'}, user=self.dm_a)
        self.assertEqual(response.status_code, 403)

    def test_cross_tenant_update_is_rejected(self):
        response = self.patch_department(self.department_a.id, {'description': 'Hijacked'}, user=self.owner_b)
        self.assertEqual(response.status_code, 403)

    def test_renaming_to_an_existing_name_is_rejected(self):
        response = self.patch_department(self.department_a.id, {'name': 'Design'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.json()['errors'])

    def test_renaming_to_its_own_current_name_is_not_a_conflict(self):
        response = self.patch_department(self.department_a.id, {'name': 'Engineering', 'description': 'Same name'})
        self.assertEqual(response.status_code, 200)

    def assign_leader(self, department_id, user_id, actor=None):
        return self.client.post(
            f'/api/departments/{department_id}/leader/', json.dumps({'user_id': str(user_id)}),
            content_type='application/json', **auth_header(actor or self.owner_a),
        )

    def revoke_leader(self, department_id, actor=None):
        return self.client.delete(f'/api/departments/{department_id}/leader/', **auth_header(actor or self.owner_a))

    def test_assigning_a_plain_member_as_leader_promotes_them_to_dl_and_moves_their_department(self):
        response = self.assign_leader(self.other_department.id, self.dm_a.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['department']['leader_id'], str(self.dm_a.id))
        profile = CompanyUserProfile.objects.get(user=self.dm_a, company=self.company_a)
        self.assertEqual(profile.role, CompanyUserProfile.Role.DEPARTMENT_LEADER)
        self.assertEqual(profile.department_id, self.other_department.id)

    def test_assigning_a_company_manager_as_leader_leaves_their_role_untouched(self):
        cm = self._make_profile('cm-a', CompanyUserProfile.Role.COMPANY_MANAGER)
        response = self.assign_leader(self.other_department.id, cm.id)
        self.assertEqual(response.status_code, 200)
        profile = CompanyUserProfile.objects.get(user=cm, company=self.company_a)
        self.assertEqual(profile.role, CompanyUserProfile.Role.COMPANY_MANAGER)

    def test_revoking_leadership_reverts_role_to_department_member(self):
        self.assign_leader(self.other_department.id, self.dm_a.id)
        response = self.revoke_leader(self.other_department.id)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['data']['department']['leader_id'])
        profile = CompanyUserProfile.objects.get(user=self.dm_a, company=self.company_a)
        self.assertEqual(profile.role, CompanyUserProfile.Role.DEPARTMENT_MEMBER)

    def test_replacing_a_leader_who_still_leads_elsewhere_keeps_their_dl_role(self):
        # dl_a already leads department_a (Department.leader, not just the
        # profile role); naming them leader of a second department and then
        # replacing them there must not demote them, since they still lead
        # department_a.
        self.department_a.leader = self.dl_a
        self.department_a.save(update_fields=['leader'])
        self.assign_leader(self.other_department.id, self.dl_a.id)
        someone_else = self._make_profile('someone-else', CompanyUserProfile.Role.DEPARTMENT_MEMBER)
        self.assign_leader(self.other_department.id, someone_else.id)
        profile = CompanyUserProfile.objects.get(user=self.dl_a, company=self.company_a)
        self.assertEqual(profile.role, CompanyUserProfile.Role.DEPARTMENT_LEADER)

    def test_assigning_leader_from_another_company_is_rejected(self):
        response = self.assign_leader(self.other_department.id, self.owner_b.id)
        self.assertEqual(response.status_code, 400)

    def test_only_manager_can_assign_leader(self):
        response = self.assign_leader(self.other_department.id, self.dm_a.id, actor=self.dm_a)
        self.assertEqual(response.status_code, 403)

    def test_requires_authentication(self):
        response = self.client.patch(
            f'/api/departments/{self.department_a.id}/', json.dumps({'description': 'x'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)


class TeamTests(TwoCompanyTestCase):
    """Teams mix members across departments for a specific initiative --
    unlike a Department, membership isn't a fixed org placement."""

    def setUp(self):
        super().setUp()
        self.design_department = Department.objects.create(name='Design', company=self.company_a)
        self.designer_a = self._make_member(self.design_department)
        self.cm_a = User.objects.create_user(email='cm-a@example.com', username='cm-a', password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(
            user=self.cm_a, company=self.company_a, role=CompanyUserProfile.Role.COMPANY_MANAGER,
        )

    def _make_member(self, department):
        from users.models import User

        user = User.objects.create_user(
            email=f'{department.name.lower()}-member@example.com',
            username=f'{department.name.lower()}-member', password='Kx9#mQ2vLp8Z',
        )
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, department=department,
            role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        return user

    def create_team(self, user, **overrides):
        body = {'name': 'Launch Task Force', 'member_ids': [str(self.member_a.id), str(self.designer_a.id)]}
        body.update(overrides)
        return self.client.post(
            '/api/teams/', json.dumps(body), content_type='application/json', **auth_header(user),
        )

    def list_teams(self, user):
        return self.client.get('/api/teams/', **auth_header(user))

    def test_owner_can_create_team_mixing_members_from_different_departments(self):
        response = self.create_team(self.owner_a)
        self.assertEqual(response.status_code, 201)
        data = response.json()['data']['team']
        self.assertEqual(set(data['member_ids']), {str(self.member_a.id), str(self.designer_a.id)})
        team = Team.objects.get(company=self.company_a, name='Launch Task Force')
        self.assertEqual(
            set(team.members.values_list('id', flat=True)), {self.member_a.id, self.designer_a.id},
        )

    def test_department_member_cannot_create_team(self):
        response = self.create_team(self.member_a)
        self.assertEqual(response.status_code, 403)

    def test_company_manager_can_create_team(self):
        response = self.create_team(self.cm_a)
        self.assertEqual(response.status_code, 201)

    def test_member_from_another_company_is_rejected(self):
        response = self.create_team(self.owner_a, member_ids=[str(self.owner_b.id)])
        self.assertEqual(response.status_code, 400)
        self.assertIn('member_ids', response.json()['errors'])

    def test_duplicate_team_name_within_company_is_rejected(self):
        self.create_team(self.owner_a)
        response = self.create_team(self.owner_a)
        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.json()['errors'])

    def test_teams_are_scoped_to_the_caller_own_company(self):
        self.create_team(self.owner_a)
        response = self.list_teams(self.owner_b)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['results'], [])

    def test_member_can_list_teams(self):
        self.create_team(self.owner_a)
        response = self.list_teams(self.member_a)
        self.assertEqual(response.status_code, 200)
        names = {item['name'] for item in response.json()['data']['results']}
        self.assertEqual(names, {'Launch Task Force'})

    def test_requires_authentication(self):
        response = self.client.get('/api/teams/')
        self.assertEqual(response.status_code, 401)


class TeamUpdateTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.team = Team.objects.create(name='Launch Squad', company=self.company_a)

    def patch_team(self, body, user=None):
        return self.client.patch(
            f'/api/teams/{self.team.id}/', json.dumps(body), content_type='application/json',
            **auth_header(user or self.owner_a),
        )

    def assign_leader(self, user_id, actor=None):
        return self.client.post(
            f'/api/teams/{self.team.id}/leader/', json.dumps({'user_id': str(user_id)}),
            content_type='application/json', **auth_header(actor or self.owner_a),
        )

    def test_owner_can_rename_team(self):
        response = self.patch_team({'name': 'Renamed Squad'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['team']['name'], 'Renamed Squad')

    def test_member_cannot_update_team(self):
        response = self.patch_team({'description': 'Hijacked'}, user=self.member_a)
        self.assertEqual(response.status_code, 403)

    def test_cross_tenant_update_is_rejected(self):
        response = self.patch_team({'description': 'Hijacked'}, user=self.owner_b)
        self.assertEqual(response.status_code, 403)

    def test_assigning_a_team_leader_does_not_change_their_role(self):
        """Team leadership is a plain label -- no authorization keys off it,
        unlike department leadership."""
        response = self.assign_leader(self.member_a.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['team']['leader_id'], str(self.member_a.id))
        profile = CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a)
        self.assertEqual(profile.role, CompanyUserProfile.Role.DEPARTMENT_MEMBER)

    def test_revoking_team_leader(self):
        self.assign_leader(self.member_a.id)
        response = self.client.delete(f'/api/teams/{self.team.id}/leader/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['data']['team']['leader_id'])

    def test_assigning_leader_from_another_company_is_rejected(self):
        response = self.assign_leader(self.owner_b.id)
        self.assertEqual(response.status_code, 400)


class DefaultConfigTests(TwoCompanyTestCase):
    """Post-registration default-department management: reuses the exact
    same apply_default_departments dedupe logic as onboarding, and adds
    traceability (default_department) without affecting pre-existing rows.
    """

    def setUp(self):
        super().setUp()
        self.default = DefaultDepartment.objects.create(name='Marketing', sector=None)

    def list_config(self, user=None):
        return self.client.get('/api/company/default-config/', **auth_header(user or self.owner_a))

    def enable(self, selected_ids=None, use_all=False, user=None):
        body = {'use_all': use_all}
        if selected_ids is not None:
            body['selected_ids'] = [str(i) for i in selected_ids]
        return self.client.post(
            '/api/company/default-config/departments/', json.dumps(body),
            content_type='application/json', **auth_header(user or self.owner_a),
        )

    def test_default_starts_disabled(self):
        response = self.list_config()
        self.assertEqual(response.status_code, 200)
        by_name = {d['name']: d for d in response.json()['data']['departments']}
        self.assertFalse(by_name['Marketing']['enabled'])

    def test_enabling_a_default_creates_a_traceable_department(self):
        response = self.enable(selected_ids=[self.default.id])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['data']['total_created'], 1)
        department = Department.objects.get(company=self.company_a, name='Marketing')
        self.assertEqual(department.default_department_id, self.default.id)

        status_response = self.list_config()
        by_name = {d['name']: d for d in status_response.json()['data']['departments']}
        self.assertTrue(by_name['Marketing']['enabled'])

    def test_reenabling_an_already_enabled_default_is_a_noop(self):
        self.enable(selected_ids=[self.default.id])
        response = self.enable(selected_ids=[self.default.id])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['data']['total_created'], 0)
        self.assertEqual(Department.objects.filter(company=self.company_a, name='Marketing').count(), 1)

    def test_manually_created_department_has_no_default_traceability(self):
        # department_a (Engineering) is created directly by TwoCompanyTestCase.setUp,
        # not through apply_default_departments -- must remain untraceable.
        self.assertIsNone(self.department_a.default_department_id)

    def test_department_member_cannot_manage_default_config(self):
        response = self.enable(selected_ids=[self.default.id], user=self.member_a)
        self.assertEqual(response.status_code, 403)

    def test_scoped_to_the_callers_own_company(self):
        self.enable(selected_ids=[self.default.id])
        response = self.list_config(user=self.owner_b)
        by_name = {d['name']: d for d in response.json()['data']['departments']}
        self.assertFalse(by_name['Marketing']['enabled'])

    def test_requires_authentication(self):
        response = self.client.get('/api/company/default-config/')
        self.assertEqual(response.status_code, 401)
