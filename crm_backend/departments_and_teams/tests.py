"""Department/team directory API: happy path, membership requirement, and
cross-tenant rejection, reusing the TwoCompanyTestCase fixture (api/tests.py)
so isolation is checked the same way every other endpoint's tests check it.
"""

import json

from api.tests import TwoCompanyTestCase, auth_header
from users.models import CompanyUserProfile, User

from departments_and_teams.models import Department, Team


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
