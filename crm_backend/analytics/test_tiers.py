"""Who may see which analytics.

The endpoint §8 singles out is `/analytics/company/members/` -- every
colleague's name against their open task counts, previously readable by every
role including a Department Member. That is one join away from a performance
dashboard, and §8 is explicit that Workroom does not build one.

The tiers widen from "your own numbers" to "everyone's", and the boundaries
fall wherever a number stops being about a project and starts being about a
person.
"""

from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from django.utils import timezone
from projects_and_tasks.models import Project, ProjectMembership, Task
from users.models import CompanyUserProfile, User

from analytics import tiers

PASSWORD = 'Kx9#mQ2vLp8Z'


class TierWorldMixin:
    def setUp(self):
        super().setUp()
        self.cm = self._member('cm', CompanyUserProfile.Role.COMPANY_MANAGER)
        self.dl = self._member('dl', CompanyUserProfile.Role.DEPARTMENT_LEADER, self.department_a)
        now = timezone.now()
        self.project = Project.objects.create(
            title='Website Revamp', company=self.company_a, department=self.department_a,
            visibility=Project.VISIBILITY.COMPANY, start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )
        Task.objects.create(
            project=self.project, title='Design', assigned_to=self.member_a,
            created_by=self.owner_a, deadline=now + timedelta(days=10),
        )

    def _member(self, name, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER, department=None):
        user = User.objects.create_user(email=f'{name}@example.com', username=name, password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, department=department or self.department_a, role=role,
        )
        return user

    def get(self, path, actor):
        return self.client.get(path, **auth_header(actor))


class PersonalTierTests(TierWorldMixin, TwoCompanyTestCase):
    """Never gated. Seeing your own workload is not a privilege, and making it
    one would push people to the company roster to answer a question about
    themselves."""

    def test_any_member_can_read_their_own_workload(self):
        response = self.get('/api/v1/analytics/me/', self.member_a)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['active_task_count'], 1)

    def test_it_reports_only_your_own(self):
        response = self.get('/api/v1/analytics/me/', self.cm)
        self.assertEqual(response.json()['data']['active_task_count'], 0)


class CompanyTierTests(TierWorldMixin, TwoCompanyTestCase):
    """The roster §8 singles out, and the company figures beside it."""

    def test_a_department_member_can_no_longer_read_the_roster(self):
        response = self.get('/api/v1/analytics/company/members/', self.member_a)
        self.assertEqual(response.status_code, 403, response.content)

    def test_a_department_leader_cannot_either(self):
        """A DL manages a department, not the company. Their tier is the
        department one."""
        response = self.get('/api/v1/analytics/company/members/', self.dl)
        self.assertEqual(response.status_code, 403, response.content)

    def test_a_company_manager_can(self):
        response = self.get('/api/v1/analytics/company/members/', self.cm)
        self.assertEqual(response.status_code, 200, response.content)

    def test_the_owner_can(self):
        response = self.get('/api/v1/analytics/company/members/', self.owner_a)
        self.assertEqual(response.status_code, 200, response.content)

    def test_company_stats_follow_the_same_tier(self):
        self.assertEqual(self.get('/api/v1/analytics/company/', self.member_a).status_code, 403)
        self.assertEqual(self.get('/api/v1/analytics/company/', self.owner_a).status_code, 200)

    def test_another_companys_owner_sees_their_own_company_not_this_one(self):
        """These endpoints take no company id -- it is derived from the
        caller. So the cross-tenant question is not "is it refused" but "whose
        company did it answer about", and the answer must be theirs."""
        response = self.get('/api/v1/analytics/company/members/', self.owner_b)
        self.assertEqual(response.status_code, 200, response.content)
        ids = {member['id'] for member in response.json()['data']['members']}
        self.assertIn(str(self.owner_b.id), ids)
        self.assertNotIn(str(self.member_a.id), ids)
        self.assertNotIn(str(self.owner_a.id), ids)

    def test_the_refusal_says_who_may(self):
        """A bare 403 on an analytics page reads as a bug. Naming the roles
        makes it a rule."""
        response = self.get('/api/v1/analytics/company/members/', self.member_a)
        self.assertIn('Company Manager', response.json()['message'])


class DepartmentTierTests(TierWorldMixin, TwoCompanyTestCase):
    def test_the_whole_company_breakdown_is_company_tier(self):
        """Without a department id this is every department's numbers, which a
        Department Leader must not read through a different door."""
        self.assertEqual(self.get('/api/v1/analytics/company/departments/', self.dl).status_code, 403)
        self.assertEqual(self.get('/api/v1/analytics/company/departments/', self.owner_a).status_code, 200)

    def test_a_leader_may_ask_about_their_own_department(self):
        response = self.get(
            f'/api/v1/analytics/company/departments/?department_id={self.department_a.id}', self.dl,
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()['data']['departments']
        self.assertEqual([row['id'] for row in rows], [str(self.department_a.id)])

    def test_a_leader_may_not_ask_about_another_department(self):
        other = self.department_a.__class__.objects.create(name='Sales', company=self.company_a)
        response = self.get(
            f'/api/v1/analytics/company/departments/?department_id={other.id}', self.dl,
        )
        self.assertEqual(response.status_code, 403, response.content)

    def test_a_department_member_may_not_ask_at_all(self):
        response = self.get(
            f'/api/v1/analytics/company/departments/?department_id={self.department_a.id}', self.member_a,
        )
        self.assertEqual(response.status_code, 403, response.content)


class ProjectTierTests(TierWorldMixin, TwoCompanyTestCase):
    def test_aggregate_figures_need_only_project_view(self):
        response = self.get(f'/api/v1/analytics/projects/{self.project.id}/', self.member_a)
        self.assertEqual(response.status_code, 200, response.content)

    def test_aggregate_figures_carry_no_names(self):
        """A project's completion rate is a fact about the work. Who is behind
        on it is a fact about a person, and belongs one tier up."""
        response = self.get(f'/api/v1/analytics/projects/{self.project.id}/', self.member_a)
        body = response.json()['data']
        self.assertNotIn('members', body)
        for key in body:
            self.assertNotIn('name', key)

    def test_per_member_figures_need_project_manage(self):
        response = self.get(f'/api/v1/analytics/projects/{self.project.id}/members/', self.member_a)
        self.assertEqual(response.status_code, 403, response.content)

    def test_a_project_manager_sees_per_member_figures(self):
        response = self.get(f'/api/v1/analytics/projects/{self.project.id}/members/', self.owner_a)
        self.assertEqual(response.status_code, 200, response.content)
        members = response.json()['data']['members']
        self.assertEqual([row['id'] for row in members], [str(self.member_a.id)])

    def test_a_project_manager_membership_is_enough(self):
        ProjectMembership.objects.create(
            project=self.project, user=self.member_a,
            role=ProjectMembership.Role.MANAGER, added_by=self.owner_a,
        )
        response = self.get(f'/api/v1/analytics/projects/{self.project.id}/members/', self.member_a)
        self.assertEqual(response.status_code, 200, response.content)

    def test_per_member_figures_count_only_this_projects_tasks(self):
        """Showing someone's company-wide load here would leak the workload of
        projects the viewer has nothing to do with, through one they manage."""
        other_project = Project.objects.create(
            title='Mobile App', company=self.company_a, start_date=timezone.now(),
            deadline=timezone.now() + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )
        Task.objects.create(
            project=other_project, title='Elsewhere', assigned_to=self.member_a,
            created_by=self.owner_a, deadline=timezone.now() + timedelta(days=10),
        )
        response = self.get(f'/api/v1/analytics/projects/{self.project.id}/members/', self.owner_a)
        member = response.json()['data']['members'][0]
        self.assertEqual(member['active_task_count'], 1)

    def test_another_company_cannot_reach_project_figures(self):
        response = self.get(f'/api/v1/analytics/projects/{self.project.id}/', self.owner_b)
        self.assertIn(response.status_code, (403, 404))


class NoPerPersonRatesTests(TierWorldMixin, TwoCompanyTestCase):
    """§8 rules out per-person on-time percentages, velocity, productivity
    scores and rankings -- "not now, not later without a separate explicit
    decision". This pins their absence so adding one is a deliberate act that
    breaks a test, rather than a quiet addition to a serializer."""

    FORBIDDEN_KEYS = (
        'on_time', 'on_time_percent', 'velocity', 'productivity', 'score', 'rank', 'rating',
        'efficiency', 'performance',
    )

    def assert_no_rates(self, body):
        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    lowered = key.lower()
                    for forbidden in self.FORBIDDEN_KEYS:
                        self.assertNotIn(
                            forbidden, lowered,
                            f'{key!r} looks like a per-person performance metric',
                        )
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(body)

    def test_the_company_roster_carries_none(self):
        response = self.get('/api/v1/analytics/company/members/', self.owner_a)
        self.assert_no_rates(response.json()['data'])

    def test_project_per_member_figures_carry_none(self):
        response = self.get(f'/api/v1/analytics/projects/{self.project.id}/members/', self.owner_a)
        self.assert_no_rates(response.json()['data'])

    def test_personal_figures_carry_none(self):
        response = self.get('/api/v1/analytics/me/', self.member_a)
        self.assert_no_rates(response.json()['data'])


class TierPredicateTests(TierWorldMixin, TwoCompanyTestCase):
    """The predicates directly, so a future endpoint reaching for one gets the
    same answer the endpoints here do."""

    def test_company_tier(self):
        self.assertTrue(async_to_sync(tiers.can_view_company_analytics)(self.owner_a, self.company_a))
        self.assertTrue(async_to_sync(tiers.can_view_company_analytics)(self.cm, self.company_a))
        self.assertFalse(async_to_sync(tiers.can_view_company_analytics)(self.dl, self.company_a))
        self.assertFalse(async_to_sync(tiers.can_view_company_analytics)(self.member_a, self.company_a))

    def test_department_tier(self):
        check = async_to_sync(tiers.can_view_department_analytics)
        self.assertTrue(check(self.dl, self.company_a, self.department_a.id))
        self.assertFalse(check(self.dl, self.company_a, None))
        self.assertTrue(check(self.owner_a, self.company_a, None))
        self.assertFalse(check(self.member_a, self.company_a, self.department_a.id))

    def test_project_tiers(self):
        self.assertTrue(async_to_sync(tiers.can_view_project_people)(self.owner_a, self.project))
        self.assertFalse(async_to_sync(tiers.can_view_project_people)(self.member_a, self.project))
        self.assertTrue(async_to_sync(tiers.can_view_project_aggregate)(self.member_a, self.project))
        self.assertFalse(async_to_sync(tiers.can_view_project_aggregate)(self.owner_b, self.project))
