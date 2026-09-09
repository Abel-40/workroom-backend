"""The allow-list boundary: exactly what leaves the building, and nothing
more.

This is the security-critical half of §4. The other tests in this app cover
the pipeline's behaviour; these cover its contract -- that the payload built
for the AI service literally cannot carry a forbidden field, whatever gets
added to a model later, because the serializer names every field it sends
rather than naming what it withholds.
"""

from datetime import timedelta

from api.tests import TwoCompanyTestCase
from departments_and_teams.models import Department
from django.utils import timezone
from projects_and_tasks.models import Project, TaskType
from users.models import CompanyUserProfile, User
from workforce.models import AssignmentPolicy, MemberSkill, Profession, Skill

from ai_agent.context import (
    CANDIDATE_FIELDS,
    CONTEXT_FIELDS,
    build_assignee_refs,
    build_candidate_pool,
    build_generation_context,
    estimate_cost,
    resolve_assignee_ref,
)
from ai_agent.models import AIGeneration

PASSWORD = 'Kx9#mQ2vLp8Z'

# Everything the brief says must never reach the AI service. Populated on the
# fixture with a findable sentinel so a leak shows up as a substring match,
# not as a guess about which field it came from.
FORBIDDEN_SENTINELS = {
    'email': 'forbidden-sentinel@leak.example.com',
    'address': 'FORBIDDEN_SENTINEL_ADDRESS_221B_BAKER_ST',
    'phone_number': 'FRBDN-555-0100',
    'skype': 'FORBIDDEN_SENTINEL_SKYPE_HANDLE',
}
# NOT forbidden, deliberately excluded from the sentinel set above:
# ``username``. ``display_name`` is itself an allow-listed candidate field
# (§4), and it falls back to username when a person has set no first/last
# name -- that fallback is the designed behaviour of an allowed field, not a
# leak of a withheld one.


class ContextFixture(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        # TwoCompanyTestCase already creates self.department_a ('Engineering'
        # in company_a); reuse it rather than colliding with its unique
        # (company, name) constraint.
        self.department = self.department_a
        self.task_type = TaskType.objects.create(name='Development', company=self.company_a)
        self.skill = Skill.objects.create(company=self.company_a, name='Python', category='Engineering')
        self.profession = Profession.objects.create(company=self.company_a, name='Backend Engineer')
        self.project = Project.objects.create(
            title='Support platform', description='A SaaS platform.', company=self.company_a,
            created_by=self.owner_a, deadline=timezone.now() + timedelta(days=90),
        )
        self.candidate = User.objects.create_user(
            email=FORBIDDEN_SENTINELS['email'], username='candidate', password=PASSWORD,
        )
        self.candidate.first_name = 'Cam'
        self.candidate.save(update_fields=['first_name'])
        self.candidate_profile = CompanyUserProfile.objects.create(
            user=self.candidate, company=self.company_a, department=self.department,
            role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
            address=FORBIDDEN_SENTINELS['address'], phone_number=FORBIDDEN_SENTINELS['phone_number'],
            skype=FORBIDDEN_SENTINELS['skype'], profession_ref=self.profession, weekly_capacity_hours=40,
        )
        MemberSkill.objects.create(profile=self.candidate_profile, skill=self.skill, level='strong')

    def make_generation(self, **overrides):
        fields = {
            'project': self.project, 'requested_by': self.owner_a, 'prompt': 'Build it',
            'requested_assignee_ids': [str(self.candidate.id)],
            'assignee_refs': build_assignee_refs([self.candidate.id]),
        }
        fields.update(overrides)
        return AIGeneration.objects.create(**fields)


class AllowListBoundaryTests(ContextFixture):
    """The payload literally cannot carry a forbidden field."""

    def test_no_forbidden_sentinel_appears_anywhere_in_the_payload(self):
        import json

        generation = self.make_generation()
        context = build_generation_context(generation)
        serialized = json.dumps(context)
        for field, sentinel in FORBIDDEN_SENTINELS.items():
            self.assertNotIn(sentinel, serialized, f'{field} leaked into the AI context payload')

    def test_the_top_level_shape_is_exactly_the_allow_list(self):
        generation = self.make_generation()
        context = build_generation_context(generation)
        # No brief row exists for this project, so the key is absent
        # entirely -- not null, absent, which is the honest shape for
        # "there is no brief" (see test_a_briefs_prose_fields_are_sent below
        # for the case where one exists).
        self.assertNotIn('brief', context)
        self.assertTrue(set(context).issubset(CONTEXT_FIELDS))

    def test_a_briefs_prose_fields_are_sent_once_one_exists(self):
        from projects_and_tasks.models import ProjectBrief

        ProjectBrief.objects.create(
            project=self.project, objective='Ship it', background='Old system is slow',
            scope_in='API', scope_out='Mobile app', expected_outcome='Faster checkout',
            constraints='No downtime',
        )
        generation = self.make_generation()
        context = build_generation_context(generation)
        self.assertEqual(context['brief'], {
            'objective': 'Ship it', 'background': 'Old system is slow', 'scope_in': 'API',
            'scope_out': 'Mobile app', 'expected_outcome': 'Faster checkout', 'constraints': 'No downtime',
        })

    def test_the_briefs_body_field_never_reaches_the_payload(self):
        """`body` is the deliberately unstructured remainder --
        deliverables/stakeholders/resources shaped however a company likes.
        Not on the allow-list: an open JSON blob is exactly the kind of field
        an allow-list exists to keep out until someone deliberately adds it."""
        import json

        from projects_and_tasks.models import ProjectBrief

        ProjectBrief.objects.create(
            project=self.project, objective='Ship it',
            body={'stakeholders': ['forbidden-sentinel-stakeholder']},
        )
        generation = self.make_generation()
        context = build_generation_context(generation)
        self.assertNotIn('forbidden-sentinel-stakeholder', json.dumps(context))

    def test_a_candidate_entry_carries_only_the_allow_listed_fields(self):
        generation = self.make_generation()
        context = build_generation_context(generation)
        self.assertEqual(len(context['candidates']), 1)
        candidate = context['candidates'][0]
        self.assertEqual(set(candidate), CANDIDATE_FIELDS)

    def test_a_real_user_id_never_appears_in_the_payload(self):
        import json

        generation = self.make_generation()
        context = build_generation_context(generation)
        serialized = json.dumps(context)
        self.assertNotIn(str(self.candidate.id), serialized)
        self.assertNotIn(str(self.owner_a.id), serialized)

    def test_a_payload_with_an_extra_key_is_refused_even_from_inside_this_module(self):
        """The guard runs, not just the shape of the allow-list constant."""
        from ai_agent import context as context_module

        with self.assertRaises(ValueError):
            context_module._assert_shape({**dict.fromkeys(CONTEXT_FIELDS, None), 'billing_plan': 'enterprise'})

    def test_a_candidate_with_an_extra_key_is_refused(self):
        from ai_agent import context as context_module

        shape = {field: None for field in CONTEXT_FIELDS if field != 'candidates'}
        shape['candidates'] = [{**dict.fromkeys(CANDIDATE_FIELDS, None), 'phone_number': '555'}]
        with self.assertRaises(ValueError):
            context_module._assert_shape(shape)


class OpaqueRefTests(ContextFixture):
    def test_refs_are_sequential_and_do_not_encode_the_user_id(self):
        second = User.objects.create_user(email='b@example.com', username='b', password=PASSWORD)
        refs = build_assignee_refs([self.candidate.id, second.id])
        self.assertEqual(set(refs), {'member_1', 'member_2'})
        for ref in refs:
            self.assertNotIn(str(self.candidate.id), ref)
            self.assertNotIn(str(second.id), ref)

    def test_a_ref_resolves_only_through_its_own_generation(self):
        generation_a = self.make_generation()
        generation_b = self.make_generation(assignee_refs={'member_1': str(self.owner_a.id)})
        self.assertEqual(resolve_assignee_ref(generation_a, 'member_1'), str(self.candidate.id))
        # Same ref token, different generation, different person -- refs carry
        # no meaning outside the plan that minted them.
        self.assertEqual(resolve_assignee_ref(generation_b, 'member_1'), str(self.owner_a.id))

    def test_an_unknown_ref_resolves_to_none(self):
        generation = self.make_generation()
        self.assertIsNone(resolve_assignee_ref(generation, 'member_99'))
        self.assertIsNone(resolve_assignee_ref(generation, 'not-a-ref-at-all'))
        self.assertIsNone(resolve_assignee_ref(generation, ''))
        self.assertIsNone(resolve_assignee_ref(generation, None))


class CandidatePoolTests(ContextFixture):
    def test_a_non_member_of_this_company_is_never_a_candidate(self):
        """Even if their id somehow ends up in the requested pool -- the
        candidate must be a real, active member of *this* company."""
        outsider = User.objects.create_user(email='out@example.com', username='out', password=PASSWORD)
        refs = build_assignee_refs([self.candidate.id, outsider.id])
        pool = build_candidate_pool(self.company_a, [self.candidate.id, outsider.id], refs)
        self.assertEqual(len(pool), 1)
        candidate_ref = next(ref for ref, uid in refs.items() if uid == str(self.candidate.id))
        self.assertEqual(pool[0]['opaque_ref'], candidate_ref)

    def test_a_deactivated_member_is_never_a_candidate(self):
        self.candidate_profile.is_active = False
        self.candidate_profile.save(update_fields=['is_active'])
        refs = build_assignee_refs([self.candidate.id])
        pool = build_candidate_pool(self.company_a, [self.candidate.id], refs)
        self.assertEqual(pool, [])

    def test_profession_skills_and_workload_are_included(self):
        refs = build_assignee_refs([self.candidate.id])
        pool = build_candidate_pool(self.company_a, [self.candidate.id], refs)
        entry = pool[0]
        self.assertEqual(entry['profession'], 'Backend Engineer')
        self.assertEqual(entry['skills'], [{'name': 'Python', 'level': 'strong'}])
        self.assertEqual(entry['department'], 'Engineering')
        self.assertEqual(entry['capacity_hours'], 80.0)  # 40h/week x 2 (14-day horizon)
        self.assertEqual(entry['committed_hours'], 0.0)
        self.assertEqual(entry['active_task_count'], 0)

    def test_an_unstated_capacity_is_null_not_zero(self):
        self.candidate_profile.weekly_capacity_hours = None
        self.candidate_profile.save(update_fields=['weekly_capacity_hours'])
        refs = build_assignee_refs([self.candidate.id])
        pool = build_candidate_pool(self.company_a, [self.candidate.id], refs)
        self.assertIsNone(pool[0]['capacity_hours'])

    def test_a_profession_less_skill_less_candidate_still_appears(self):
        """Being unrecommended by workforce.is_eligible_for_ai_recommendation
        is a separate concern (WP10) from being a valid candidate here -- the
        human approved the pool, and the model still gets to see them."""
        bare = User.objects.create_user(email='bare@example.com', username='bare', password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=bare, company=self.company_a, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        refs = build_assignee_refs([bare.id])
        pool = build_candidate_pool(self.company_a, [bare.id], refs)
        self.assertEqual(len(pool), 1)
        self.assertIsNone(pool[0]['profession'])
        self.assertEqual(pool[0]['skills'], [])


class AssignmentLimitsTests(ContextFixture):
    def test_no_policy_reports_off(self):
        generation = self.make_generation()
        context = build_generation_context(generation)
        self.assertEqual(context['assignment_limits'], {
            'enforcement': 'off', 'max_active_tasks': None, 'max_utilisation_pct': None,
        })

    def test_a_configured_policy_is_reported(self):
        AssignmentPolicy.objects.create(
            company=self.company_a, enabled=True, max_active_tasks=5,
            max_utilisation_pct=80, enforcement='warn',
        )
        generation = self.make_generation()
        context = build_generation_context(generation)
        self.assertEqual(context['assignment_limits'], {
            'enforcement': 'warn', 'max_active_tasks': 5, 'max_utilisation_pct': 80,
        })


class DepartmentTaskTypeSkillCatalogTests(ContextFixture):
    def test_only_names_are_sent_never_ids(self):
        import json

        generation = self.make_generation()
        context = build_generation_context(generation)
        self.assertEqual(context['departments'], ['Engineering'])
        self.assertEqual(context['task_types'], ['Development'])
        self.assertEqual(context['skill_catalog'], ['Python'])
        serialized = json.dumps(context)
        self.assertNotIn(str(self.department.id), serialized)
        self.assertNotIn(str(self.task_type.id), serialized)

    def test_cross_tenant_catalogs_never_leak_in(self):
        Department.objects.create(name='Other Co Dept', company=self.company_b)
        generation = self.make_generation()
        context = build_generation_context(generation)
        self.assertNotIn('Other Co Dept', context['departments'])


class CostEstimationTests(TwoCompanyTestCase):
    def test_a_known_model_prices_the_call(self):
        cost = estimate_cost('gemini', 'gemini-2.0-flash', 1_000_000, 1_000_000)
        self.assertAlmostEqual(float(cost), 0.10 + 0.40, places=6)

    def test_an_unknown_model_returns_none_not_zero(self):
        """'we don't know' and 'it was free' are different facts -- a zero
        would quietly under-report a real bill."""
        self.assertIsNone(estimate_cost('some-new-provider', 'some-new-model', 1000, 1000))

    def test_zero_tokens_still_prices_at_zero_for_a_known_model(self):
        self.assertEqual(estimate_cost('gemini', 'gemini-2.0-flash', 0, 0), 0)

    def test_matching_is_case_insensitive(self):
        cost = estimate_cost('GEMINI', 'Gemini-2.0-Flash', 1_000_000, 0)
        self.assertIsNotNone(cost)
