"""ProjectBrief (§1): a project's structured "what and why", separate from
creation.

The rules worth pinning: VIEW is enough to read it (it's context, not a
capability); MANAGE is required to write it; it's created empty on first
read rather than requiring a separate setup step; department/skill
references are validated against the caller's own company; and the
completeness score reflects prose fields plus body, never the two catalog
M2Ms (whose natural size varies too much per project to score fairly).
"""

import json
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from departments_and_teams.models import Department
from django.utils import timezone
from users.models import CompanyUserProfile, User
from workforce.models import Skill

from projects_and_tasks.models import Project, ProjectBrief, ProjectMembership

PASSWORD = 'Kx9#mQ2vLp8Z'


class BriefFixture(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.skill = Skill.objects.create(company=self.company_a, name='Python', category='Engineering')
        self.foreign_skill = Skill.objects.create(company=self.company_b, name='Python', category='Engineering')
        self.foreign_department = Department.objects.create(name='Other Dept', company=self.company_b)
        self.project = Project.objects.create(
            title='Support Platform', company=self.company_a, created_by=self.owner_a,
            current_owner=self.owner_a, visibility='company',
            deadline=timezone.now() + timedelta(days=90),
        )

    def get_brief(self, actor):
        return self.client.get(f'/api/v1/projects/{self.project.id}/brief/', **auth_header(actor))

    def patch_brief(self, actor, **body):
        return self.client.patch(
            f'/api/v1/projects/{self.project.id}/brief/', json.dumps(body),
            content_type='application/json', **auth_header(actor),
        )


class BriefAccessTests(BriefFixture):
    def test_created_empty_on_first_read(self):
        self.assertFalse(ProjectBrief.objects.filter(project=self.project).exists())
        response = self.get_brief(self.owner_a)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ProjectBrief.objects.filter(project=self.project).exists())
        self.assertEqual(response.json()['data']['brief']['objective'], '')

    def test_reading_twice_does_not_duplicate_the_row(self):
        self.get_brief(self.owner_a)
        self.get_brief(self.owner_a)
        self.assertEqual(ProjectBrief.objects.filter(project=self.project).count(), 1)

    def test_view_is_enough_to_read(self):
        """A department member with company-visibility VIEW, no membership
        row, can still read the brief -- it's project context, not a
        capability."""
        response = self.get_brief(self.member_a)
        self.assertEqual(response.status_code, 200)

    def test_cross_tenant_read_is_refused(self):
        response = self.get_brief(self.owner_b)
        self.assertEqual(response.status_code, 403)

    def test_unknown_project_is_404(self):
        response = self.client.get(
            '/api/v1/projects/00000000-0000-0000-0000-000000000000/brief/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 404)


class BriefEditAuthorityTests(BriefFixture):
    def test_manager_may_edit(self):
        response = self.patch_brief(self.owner_a, objective='Ship the new platform.')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['brief']['objective'], 'Ship the new platform.')

    def test_view_only_member_may_not_edit(self):
        """VIEW read the brief just fine; editing it is a different question,
        answered by MANAGE like every other project mutation."""
        response = self.patch_brief(self.member_a, objective='Sneaky.')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ProjectBrief.objects.filter(project=self.project, objective='Sneaky.').exists())

    def test_a_project_manager_membership_can_edit(self):
        manager = User.objects.create_user(email='pm@example.com', username='pm', password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=manager, company=self.company_a, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        ProjectMembership.objects.create(project=self.project, user=manager, role=ProjectMembership.Role.MANAGER)
        response = self.patch_brief(manager, objective='On it.')
        self.assertEqual(response.status_code, 200)

    def test_cross_tenant_edit_is_refused(self):
        response = self.patch_brief(self.owner_b, objective='Hijacked.')
        self.assertEqual(response.status_code, 403)

    def test_editing_does_not_gate_or_touch_project_creation(self):
        """§1: creation stays fast and is never blocked on the brief."""
        response = self.client.post(
            '/api/v1/projects/', json.dumps({
                'title': 'No Brief Yet', 'visibility': 'company',
                'deadline': (timezone.now() + timedelta(days=30)).isoformat(),
            }),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        project_id = response.json()['data']['project']['id']
        self.assertFalse(ProjectBrief.objects.filter(project_id=project_id).exists())


class BriefFieldTests(BriefFixture):
    def test_every_prose_field_is_writable(self):
        fields = {
            'objective': 'Why', 'background': 'Context', 'scope_in': 'In',
            'scope_out': 'Out', 'expected_outcome': 'Done means', 'constraints': 'Limits',
        }
        response = self.patch_brief(self.owner_a, **fields)
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']['brief']
        for field, value in fields.items():
            self.assertEqual(data[field], value)

    def test_body_is_a_loose_json_field(self):
        body = {'deliverables': ['API', 'Docs'], 'stakeholders': ['CTO']}
        response = self.patch_brief(self.owner_a, body=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['brief']['body'], body)

    def test_a_partial_update_leaves_other_fields_untouched(self):
        self.patch_brief(self.owner_a, objective='First')
        response = self.patch_brief(self.owner_a, background='Second')
        data = response.json()['data']['brief']
        self.assertEqual(data['objective'], 'First')
        self.assertEqual(data['background'], 'Second')


class BriefCatalogReferenceTests(BriefFixture):
    def test_required_departments_from_own_company_are_accepted(self):
        response = self.patch_brief(self.owner_a, required_department_ids=[str(self.department_a.id)])
        self.assertEqual(response.status_code, 200, response.content)
        names = {d['name'] for d in response.json()['data']['brief']['required_departments']}
        self.assertEqual(names, {'Engineering'})

    def test_a_department_from_another_company_is_refused(self):
        response = self.patch_brief(self.owner_a, required_department_ids=[str(self.foreign_department.id)])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            ProjectBrief.objects.get(project=self.project).required_departments.count(), 0,
        )

    def test_required_skills_from_own_company_are_accepted(self):
        response = self.patch_brief(self.owner_a, required_skill_ids=[str(self.skill.id)])
        self.assertEqual(response.status_code, 200, response.content)
        names = {s['name'] for s in response.json()['data']['brief']['required_skills']}
        self.assertEqual(names, {'Python'})

    def test_a_skill_from_another_company_is_refused(self):
        """Never free text (§3) -- and never another tenant's catalog either."""
        response = self.patch_brief(self.owner_a, required_skill_ids=[str(self.foreign_skill.id)])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            ProjectBrief.objects.get(project=self.project).required_skills.count(), 0,
        )

    def test_the_list_replaces_rather_than_merges(self):
        other_skill = Skill.objects.create(company=self.company_a, name='Vue', category='Engineering')
        self.patch_brief(self.owner_a, required_skill_ids=[str(self.skill.id), str(other_skill.id)])
        response = self.patch_brief(self.owner_a, required_skill_ids=[str(self.skill.id)])
        names = {s['name'] for s in response.json()['data']['brief']['required_skills']}
        self.assertEqual(names, {'Python'})

    def test_an_empty_list_clears_the_set(self):
        self.patch_brief(self.owner_a, required_skill_ids=[str(self.skill.id)])
        response = self.patch_brief(self.owner_a, required_skill_ids=[])
        self.assertEqual(response.json()['data']['brief']['required_skills'], [])


class BriefCompletenessTests(BriefFixture):
    def test_an_empty_brief_is_zero_percent(self):
        response = self.get_brief(self.owner_a)
        self.assertEqual(response.json()['data']['brief']['completeness_pct'], 0)

    def test_every_prose_field_filled_is_not_quite_full(self):
        """Six prose fields out of seven scored slots (the seventh is body)
        -> 6/7, not 100 -- filling in every question except "what will this
        actually produce" is deliberately shown as incomplete."""
        fields = {
            'objective': 'x', 'background': 'x', 'scope_in': 'x',
            'scope_out': 'x', 'expected_outcome': 'x', 'constraints': 'x',
        }
        response = self.patch_brief(self.owner_a, **fields)
        self.assertEqual(response.json()['data']['brief']['completeness_pct'], round(6 / 7 * 100))

    def test_a_non_empty_body_completes_the_last_slot(self):
        fields = {
            'objective': 'x', 'background': 'x', 'scope_in': 'x',
            'scope_out': 'x', 'expected_outcome': 'x', 'constraints': 'x',
        }
        response = self.patch_brief(self.owner_a, body={'deliverables': ['API']}, **fields)
        self.assertEqual(response.json()['data']['brief']['completeness_pct'], 100)

    def test_required_departments_and_skills_do_not_move_the_score(self):
        """Their natural size varies too much per project to score fairly --
        one project genuinely needs three departments, another needs none."""
        before = self.get_brief(self.owner_a).json()['data']['brief']['completeness_pct']
        self.patch_brief(self.owner_a, required_department_ids=[str(self.department_a.id)])
        after = self.get_brief(self.owner_a).json()['data']['brief']['completeness_pct']
        self.assertEqual(before, after)

    def test_whitespace_only_text_does_not_count_as_filled(self):
        response = self.patch_brief(self.owner_a, objective='   \n  ')
        self.assertEqual(response.json()['data']['brief']['completeness_pct'], 0)

    def test_an_empty_dict_body_does_not_count_as_filled(self):
        response = self.patch_brief(self.owner_a, body={})
        self.assertEqual(response.json()['data']['brief']['completeness_pct'], 0)

    def test_a_body_with_only_empty_values_does_not_count_as_filled(self):
        response = self.patch_brief(self.owner_a, body={'deliverables': [], 'notes': ''})
        self.assertEqual(response.json()['data']['brief']['completeness_pct'], 0)
