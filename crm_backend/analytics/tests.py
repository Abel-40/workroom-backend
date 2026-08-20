"""Phase 10: analytics endpoints -- correctness of the aggregates and
cross-tenant rejection, reusing the TwoCompanyTestCase fixture (api/tests.py)
so isolation is checked the same way every other endpoint's tests check it.
"""

from api.tests import TwoCompanyTestCase, auth_header
from django.utils import timezone
from projects_and_tasks.models import Project, Task
from users.models import CompanyUserProfile, User


class ProjectAnalyticsTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(title='Website Revamp', company=self.company_a, created_by=self.owner_a)
        # Future deadline for anything that must NOT count as overdue --
        # 'now' itself is already in the past by query time.
        future = timezone.now() + timezone.timedelta(days=7)
        past = timezone.now() - timezone.timedelta(days=1)
        Task.objects.create(project=self.project, title='Done task', status=Task.STATUS.DONE, deadline=past)
        Task.objects.create(
            project=self.project, title='In progress task', status=Task.STATUS.IN_PROGRESS, deadline=future,
        )
        Task.objects.create(project=self.project, title='Todo task', status=Task.STATUS.TODO, deadline=future)
        Task.objects.create(project=self.project, title='Overdue task', status=Task.STATUS.TODO, deadline=past)
        Task.objects.create(project=self.project, title='Deleted task', status=Task.STATUS.TODO, is_deleted=True)

    def get_stats(self, owner):
        return self.client.get(f'/api/analytics/projects/{self.project.id}/', **auth_header(owner))

    def test_project_stats_are_correct(self):
        response = self.get_stats(self.owner_a)
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['total_tasks'], 4)  # the is_deleted task is excluded
        self.assertEqual(data['completed_tasks'], 1)
        self.assertEqual(data['in_progress_tasks'], 1)
        self.assertEqual(data['todo_tasks'], 2)
        self.assertEqual(data['overdue_tasks'], 1)
        self.assertEqual(data['completion_percent'], 25.0)

    def test_project_stats_reject_other_company_owner(self):
        response = self.get_stats(self.owner_b)
        self.assertEqual(response.status_code, 403)

    def test_project_stats_require_authentication(self):
        response = self.client.get(f'/api/analytics/projects/{self.project.id}/')
        self.assertEqual(response.status_code, 401)


class CompanyAnalyticsTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.active_project = Project.objects.create(
            title='Active', company=self.company_a, created_by=self.owner_a, status=Project.STATUS.ACTIVE,
        )
        self.done_project = Project.objects.create(
            title='Done', company=self.company_a, created_by=self.owner_a, status=Project.STATUS.DONE,
        )
        Task.objects.create(project=self.active_project, title='T1', status=Task.STATUS.DONE)
        Task.objects.create(project=self.active_project, title='T2', status=Task.STATUS.TODO)

    def test_company_stats_are_correct(self):
        response = self.client.get('/api/analytics/company/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['project_count'], 2)
        self.assertEqual(data['active_projects'], 1)
        self.assertEqual(data['completed_projects'], 1)
        self.assertEqual(data['member_count'], 2)  # owner (no profile row) + member_a
        self.assertEqual(data['task_count'], 2)
        self.assertEqual(data['completed_tasks'], 1)

    def test_company_stats_are_scoped_to_the_caller_own_company(self):
        response = self.client.get('/api/analytics/company/', **auth_header(self.owner_b))
        data = response.json()['data']
        self.assertEqual(data['project_count'], 0)  # company B has no projects of its own


class CompanyWorkloadTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(title='Website Revamp', company=self.company_a, created_by=self.owner_a)
        Task.objects.create(project=self.project, title='Member active', status=Task.STATUS.TODO, assigned_to=self.member_a)
        Task.objects.create(project=self.project, title='Member done', status=Task.STATUS.DONE, assigned_to=self.member_a)
        Task.objects.create(project=self.project, title='Owner active', status=Task.STATUS.IN_PROGRESS, assigned_to=self.owner_a)
        Task.objects.create(project=self.project, title='Unassigned', status=Task.STATUS.TODO)

    def get_workload(self, user):
        return self.client.get('/api/analytics/company/members/', **auth_header(user))

    def test_workload_lists_owner_and_members_with_active_task_counts(self):
        response = self.get_workload(self.owner_a)
        self.assertEqual(response.status_code, 200)
        members = {m['id']: m for m in response.json()['data']['members']}
        self.assertEqual(len(members), 2)  # owner (no profile row) + member_a

        owner_row = members[str(self.owner_a.id)]
        self.assertEqual(owner_row['role'], 'Owner')
        self.assertEqual(owner_row['active_task_count'], 1)
        self.assertEqual(owner_row['in_progress_count'], 1)
        self.assertEqual(owner_row['todo_count'], 0)
        self.assertIsNone(owner_row['department'])

        member_row = members[str(self.member_a.id)]
        self.assertEqual(member_row['role'], 'DM')
        self.assertEqual(member_row['active_task_count'], 1)  # the Done task doesn't count
        self.assertEqual(member_row['todo_count'], 1)
        self.assertEqual(member_row['in_progress_count'], 0)
        self.assertEqual(member_row['department'], 'Engineering')

    def test_workload_is_scoped_to_the_caller_own_company(self):
        response = self.get_workload(self.owner_b)
        members = response.json()['data']['members']
        self.assertEqual(len(members), 1)  # company B has only its own owner, no shared members
        self.assertEqual(members[0]['id'], str(self.owner_b.id))
        self.assertEqual(members[0]['active_task_count'], 0)

    def test_workload_requires_authentication(self):
        response = self.client.get('/api/analytics/company/members/')
        self.assertEqual(response.status_code, 401)

    def test_workload_reports_company_manager_role(self):
        cm = User.objects.create_user(email='cm-a@example.com', username='cm-a', password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(
            user=cm, company=self.company_a, role=CompanyUserProfile.Role.COMPANY_MANAGER,
        )
        response = self.get_workload(self.owner_a)
        members = {m['id']: m for m in response.json()['data']['members']}
        self.assertEqual(members[str(cm.id)]['role'], 'CM')
