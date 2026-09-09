"""Profession and skill catalogs, member claims, and capacity.

The rules worth holding: a skill is never free text, a catalog belongs to
exactly one company, everybody may edit their own claims and nobody else's
unless they run the company, and capacity is a fact about a membership rather
than about a person.
"""

import json
from importlib import import_module

from api.tests import TwoCompanyTestCase, auth_header
from django.apps import apps as django_apps
from users.models import CompanyUserProfile, User

from workforce.models import DefaultProfession, DefaultSkill, MemberSkill, Profession, Skill
from workforce.services import validate_availability

PASSWORD = 'Kx9#mQ2vLp8Z'


class WorkforceFixture(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.manager_a = User.objects.create_user(
            email='cm-wf@example.com', username='cm-wf', password=PASSWORD,
        )
        CompanyUserProfile.objects.create(
            user=self.manager_a, company=self.company_a,
            role=CompanyUserProfile.Role.COMPANY_MANAGER,
        )
        self.python = Skill.objects.create(company=self.company_a, name='Python', category='Engineering')
        self.design = Skill.objects.create(company=self.company_a, name='UI Design', category='Design')
        self.foreign_skill = Skill.objects.create(company=self.company_b, name='Python', category='Engineering')
        self.engineer = Profession.objects.create(company=self.company_a, name='Backend Engineer')
        self.foreign_profession = Profession.objects.create(company=self.company_b, name='Backend Engineer')

    def profile_of(self, user):
        return CompanyUserProfile.objects.get(user=user, company=self.company_a)

    def get_profile(self, actor, target=None):
        target = target or actor
        return self.client.get(
            f'/api/v1/workforce/members/{target.id}/profile/', **auth_header(actor),
        )

    def put_skills(self, actor, target, skills):
        return self.client.put(
            f'/api/v1/workforce/members/{target.id}/skills/', json.dumps({'skills': skills}),
            content_type='application/json', **auth_header(actor),
        )

    def patch_profession(self, actor, target, profession_id):
        return self.client.patch(
            f'/api/v1/workforce/members/{target.id}/profession/',
            json.dumps({'profession_id': profession_id}),
            content_type='application/json', **auth_header(actor),
        )

    def patch_capacity(self, actor, target, **body):
        return self.client.patch(
            f'/api/v1/workforce/members/{target.id}/capacity/', json.dumps(body),
            content_type='application/json', **auth_header(actor),
        )


class SkillCatalogTests(WorkforceFixture):
    def test_catalog_is_scoped_to_the_callers_company(self):
        response = self.client.get('/api/v1/workforce/skills/', **auth_header(self.member_a))
        self.assertEqual(response.status_code, 200)
        names = {row['name'] for row in response.json()['data']['results']}
        self.assertEqual(names, {'Python', 'UI Design'})

        other = self.client.get('/api/v1/workforce/skills/', **auth_header(self.owner_b))
        self.assertEqual({row['id'] for row in other.json()['data']['results']}, {str(self.foreign_skill.id)})

    def test_requires_authentication(self):
        self.assertEqual(self.client.get('/api/v1/workforce/skills/').status_code, 401)

    def test_owner_and_manager_may_extend_the_catalog(self):
        for actor in (self.owner_a, self.manager_a):
            response = self.client.post(
                '/api/v1/workforce/skills/',
                json.dumps({'name': f'Skill by {actor.username}', 'category': 'Engineering'}),
                content_type='application/json', **auth_header(actor),
            )
            self.assertEqual(response.status_code, 201, response.content)

    def test_a_department_member_may_not_extend_the_catalog(self):
        response = self.client.post(
            '/api/v1/workforce/skills/', json.dumps({'name': 'Rust'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Skill.objects.filter(name='Rust').exists())

    def test_duplicate_names_are_refused_case_insensitively(self):
        """The whole point of a catalog: "Python" and "python" are one skill,
        and letting both exist is how matching data starts fragmenting."""
        response = self.client.post(
            '/api/v1/workforce/skills/', json.dumps({'name': 'python'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Skill.objects.filter(company=self.company_a, name__iexact='python').count(), 1)

    def test_two_companies_may_each_have_the_same_skill_name(self):
        """Uniqueness is per company, not global -- the catalogs are separate
        vocabularies, not one shared one."""
        self.assertEqual(Skill.objects.filter(name='Python').count(), 2)

    def test_a_new_skill_reuses_an_existing_categorys_spelling(self):
        response = self.client.post(
            '/api/v1/workforce/skills/', json.dumps({'name': 'Rust', 'category': 'engineering'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['data']['skill']['category'], 'Engineering')

    def test_filtering_by_category_and_search(self):
        by_category = self.client.get(
            '/api/v1/workforce/skills/?category=Design', **auth_header(self.member_a),
        )
        self.assertEqual({r['name'] for r in by_category.json()['data']['results']}, {'UI Design'})
        by_search = self.client.get(
            '/api/v1/workforce/skills/?search=pyth', **auth_header(self.member_a),
        )
        self.assertEqual({r['name'] for r in by_search.json()['data']['results']}, {'Python'})


class ProfessionCatalogTests(WorkforceFixture):
    def test_scoped_to_own_company(self):
        response = self.client.get('/api/v1/workforce/professions/', **auth_header(self.member_a))
        self.assertEqual({r['id'] for r in response.json()['data']['results']}, {str(self.engineer.id)})

    def test_department_member_may_not_extend_it(self):
        response = self.client.post(
            '/api/v1/workforce/professions/', json.dumps({'name': 'Architect'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 403)

    def test_duplicate_is_refused_case_insensitively(self):
        response = self.client.post(
            '/api/v1/workforce/professions/', json.dumps({'name': 'backend engineer'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)


class MemberSkillTests(WorkforceFixture):
    def test_a_member_can_record_their_own_skills(self):
        response = self.put_skills(self.member_a, self.member_a, [
            {'skill_id': str(self.python.id), 'level': 'expert'},
            {'skill_id': str(self.design.id), 'level': 'learning'},
        ])
        self.assertEqual(response.status_code, 200, response.content)
        rows = MemberSkill.objects.filter(profile=self.profile_of(self.member_a))
        self.assertEqual({(r.skill_id, r.level) for r in rows},
                         {(self.python.id, 'expert'), (self.design.id, 'learning')})

    def test_a_member_may_not_edit_somebody_elses_skills(self):
        other = User.objects.create_user(email='other@example.com', username='other', password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=other, company=self.company_a, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        response = self.put_skills(self.member_a, other, [{'skill_id': str(self.python.id)}])
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MemberSkill.objects.filter(profile__user=other).exists())

    def test_owner_and_manager_may_edit_anybodys(self):
        for actor in (self.owner_a, self.manager_a):
            response = self.put_skills(actor, self.member_a, [{'skill_id': str(self.python.id)}])
            self.assertEqual(response.status_code, 200, response.content)

    def test_a_skill_from_another_company_is_refused(self):
        response = self.put_skills(self.member_a, self.member_a, [{'skill_id': str(self.foreign_skill.id)}])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MemberSkill.objects.filter(skill=self.foreign_skill).exists())

    def test_an_unknown_level_is_refused_by_the_schema(self):
        response = self.put_skills(
            self.member_a, self.member_a, [{'skill_id': str(self.python.id), 'level': 'guru'}],
        )
        self.assertEqual(response.status_code, 422)

    def test_the_list_replaces_rather_than_merges(self):
        self.put_skills(self.member_a, self.member_a, [
            {'skill_id': str(self.python.id)}, {'skill_id': str(self.design.id)},
        ])
        self.put_skills(self.member_a, self.member_a, [{'skill_id': str(self.python.id)}])
        rows = MemberSkill.objects.filter(profile=self.profile_of(self.member_a))
        self.assertEqual([r.skill_id for r in rows], [self.python.id])

    def test_re_stating_a_skill_updates_the_level_in_place(self):
        """created_at should keep meaning "since when", so a level change is
        an update rather than a delete and re-insert."""
        self.put_skills(self.member_a, self.member_a, [
            {'skill_id': str(self.python.id), 'level': 'learning'},
        ])
        original = MemberSkill.objects.get(profile=self.profile_of(self.member_a), skill=self.python)
        self.put_skills(self.member_a, self.member_a, [
            {'skill_id': str(self.python.id), 'level': 'strong'},
        ])
        updated = MemberSkill.objects.get(profile=self.profile_of(self.member_a), skill=self.python)
        self.assertEqual(updated.id, original.id)
        self.assertEqual(updated.created_at, original.created_at)
        self.assertEqual(updated.level, 'strong')

    def test_cross_tenant_member_is_a_404(self):
        response = self.put_skills(self.owner_b, self.member_a, [{'skill_id': str(self.foreign_skill.id)}])
        self.assertEqual(response.status_code, 404)


class ProfessionAndEligibilityTests(WorkforceFixture):
    def test_a_member_with_neither_is_not_ai_eligible(self):
        profile = self.get_profile(self.member_a).json()['data']['profile']
        self.assertFalse(profile['ai_recommendation_eligible'])
        self.assertIn('profession', profile['ai_recommendation_hint'])

    def test_one_skill_is_enough(self):
        self.put_skills(self.member_a, self.member_a, [{'skill_id': str(self.python.id)}])
        profile = self.get_profile(self.member_a).json()['data']['profile']
        self.assertTrue(profile['ai_recommendation_eligible'])
        self.assertIsNone(profile['ai_recommendation_hint'])

    def test_a_profession_alone_is_enough(self):
        self.patch_profession(self.member_a, self.member_a, str(self.engineer.id))
        profile = self.get_profile(self.member_a).json()['data']['profile']
        self.assertTrue(profile['ai_recommendation_eligible'])

    def test_the_legacy_placeholder_never_counts_as_a_profession(self):
        """'Not provided' was the old column's default, so reading it would
        make every member in the database eligible."""
        profile = self.profile_of(self.member_a)
        self.assertEqual(profile.profession, 'Not provided')
        self.assertFalse(self.get_profile(self.member_a).json()['data']['profile']['ai_recommendation_eligible'])

    def test_setting_a_profession_dual_writes_the_deprecated_column(self):
        self.patch_profession(self.member_a, self.member_a, str(self.engineer.id))
        profile = self.profile_of(self.member_a)
        self.assertEqual(profile.profession_ref_id, self.engineer.id)
        self.assertEqual(profile.profession, 'Backend Engineer')

    def test_clearing_a_profession_restores_the_placeholder(self):
        self.patch_profession(self.member_a, self.member_a, str(self.engineer.id))
        self.patch_profession(self.member_a, self.member_a, None)
        profile = self.profile_of(self.member_a)
        self.assertIsNone(profile.profession_ref_id)
        self.assertEqual(profile.profession, 'Not provided')

    def test_a_profession_from_another_company_is_refused(self):
        response = self.patch_profession(
            self.member_a, self.member_a, str(self.foreign_profession.id),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.profile_of(self.member_a).profession_ref_id)


class CapacityTests(WorkforceFixture):
    def test_capacity_is_unstated_by_default(self):
        profile = self.get_profile(self.member_a).json()['data']['profile']
        self.assertIsNone(profile['weekly_capacity_hours'])
        self.assertEqual(profile['availability'], {})

    def test_a_member_can_state_their_own_capacity(self):
        response = self.patch_capacity(
            self.member_a, self.member_a, weekly_capacity_hours=32,
            availability={'working_days': ['Mon', 'tue', 'WED', 'thu']},
        )
        self.assertEqual(response.status_code, 200, response.content)
        profile = self.profile_of(self.member_a)
        self.assertEqual(profile.weekly_capacity_hours, 32)
        # Normalized to lower-case tokens in calendar order.
        self.assertEqual(profile.availability['working_days'], ['mon', 'tue', 'wed', 'thu'])

    def test_capacity_lives_on_the_membership_not_the_user(self):
        """The same person in two companies has two capacities. Writing one
        must not touch the other."""
        CompanyUserProfile.objects.create(
            user=self.member_a, company=self.company_b, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        self.patch_capacity(self.member_a, self.member_a, weekly_capacity_hours=20)
        self.assertEqual(self.profile_of(self.member_a).weekly_capacity_hours, 20)
        self.assertIsNone(
            CompanyUserProfile.objects.get(user=self.member_a, company=self.company_b).weekly_capacity_hours,
        )

    def test_a_member_may_not_set_somebody_elses_capacity(self):
        response = self.patch_capacity(self.member_a, self.owner_a, weekly_capacity_hours=80)
        self.assertEqual(response.status_code, 403)

    def test_time_off_is_stored_and_validated(self):
        ok = self.patch_capacity(self.member_a, self.member_a, availability={
            'time_off': [{'start': '2026-10-01', 'end': '2026-10-05'}],
        })
        self.assertEqual(ok.status_code, 200)
        bad = self.patch_capacity(self.member_a, self.member_a, availability={
            'time_off': [{'start': '2026-10-09', 'end': '2026-10-01'}],
        })
        self.assertEqual(bad.status_code, 400)

    def test_hours_outside_a_week_are_refused_by_the_schema(self):
        self.assertEqual(
            self.patch_capacity(self.member_a, self.member_a, weekly_capacity_hours=200).status_code, 422,
        )


class AvailabilityValidationTests(TwoCompanyTestCase):
    """The shape contract, directly. A JSONField accepts anything, so the
    contract only exists where it is checked."""

    def test_empty_shapes(self):
        self.assertEqual(validate_availability(None), ({}, None))
        self.assertEqual(validate_availability({}), ({}, None))

    def test_rejects_a_non_object(self):
        self.assertEqual(validate_availability(['mon'])[1], 'invalid_availability')

    def test_rejects_an_unknown_day(self):
        self.assertEqual(validate_availability({'working_days': ['funday']})[1], 'invalid_working_days')

    def test_rejects_an_empty_working_week(self):
        """Indistinguishable from a UI sending an empty array. Omitting the
        key is how you ask for the default week."""
        self.assertEqual(validate_availability({'working_days': []})[1], 'invalid_working_days')

    def test_deduplicates_and_orders_days(self):
        normalized, error = validate_availability({'working_days': ['fri', 'mon', 'FRI']})
        self.assertIsNone(error)
        self.assertEqual(normalized['working_days'], ['mon', 'fri'])

    def test_drops_unknown_keys(self):
        normalized, error = validate_availability({'working_days': ['mon'], 'nonsense': 1})
        self.assertIsNone(error)
        self.assertNotIn('nonsense', normalized)

    def test_rejects_a_malformed_date(self):
        self.assertEqual(
            validate_availability({'time_off': [{'start': 'soon', 'end': '2026-01-01'}]})[1], 'invalid_time_off',
        )


class ProfessionBackfillTests(WorkforceFixture):
    """Migration 0002 against the legacy free-text column.

    Invoked directly against the live app registry rather than by winding the
    migration graph backwards, matching users/test_owner_profile_backfill.py.
    """

    def setUp(self):
        super().setUp()
        self.forwards = import_module('workforce.migrations.0002_backfill_professions').backfill

    def set_legacy(self, user, value, company=None):
        CompanyUserProfile.objects.filter(
            user=user, company=company or self.company_a,
        ).update(profession=value, profession_ref=None)

    def test_a_typed_profession_becomes_a_catalog_row(self):
        self.set_legacy(self.member_a, 'Data Scientist')
        self.forwards(django_apps, None)
        profile = self.profile_of(self.member_a)
        self.assertIsNotNone(profile.profession_ref_id)
        self.assertEqual(profile.profession_ref.name, 'Data Scientist')
        self.assertEqual(profile.profession_ref.company_id, self.company_a.id)

    def test_placeholders_are_not_professions(self):
        self.set_legacy(self.member_a, 'Not provided')
        self.set_legacy(self.owner_a, '   ')
        self.forwards(django_apps, None)
        self.assertIsNone(self.profile_of(self.member_a).profession_ref_id)
        self.assertIsNone(self.profile_of(self.owner_a).profession_ref_id)
        self.assertFalse(Profession.objects.filter(name__in=['Not provided', '']).exists())

    def test_case_variants_fold_into_one_row(self):
        self.set_legacy(self.member_a, 'Data Scientist')
        self.set_legacy(self.owner_a, 'data scientist')
        self.forwards(django_apps, None)
        matches = Profession.objects.filter(company=self.company_a, name__iexact='data scientist')
        self.assertEqual(matches.count(), 1)
        self.assertEqual(self.profile_of(self.member_a).profession_ref_id, matches.first().id)
        self.assertEqual(self.profile_of(self.owner_a).profession_ref_id, matches.first().id)

    def test_the_same_title_in_two_companies_stays_two_rows(self):
        self.set_legacy(self.member_a, 'Data Scientist')
        self.set_legacy(self.owner_b, 'Data Scientist', company=self.company_b)
        self.forwards(django_apps, None)
        self.assertEqual(Profession.objects.filter(name='Data Scientist').count(), 2)

    def test_an_existing_catalog_row_is_reused(self):
        self.set_legacy(self.member_a, 'backend engineer')
        self.forwards(django_apps, None)
        self.assertEqual(self.profile_of(self.member_a).profession_ref_id, self.engineer.id)

    def test_backfill_is_idempotent(self):
        self.set_legacy(self.member_a, 'Data Scientist')
        self.forwards(django_apps, None)
        self.forwards(django_apps, None)
        self.assertEqual(Profession.objects.filter(company=self.company_a, name='Data Scientist').count(), 1)

    def test_the_old_column_is_left_alone(self):
        self.set_legacy(self.member_a, 'Data Scientist')
        self.forwards(django_apps, None)
        self.assertEqual(self.profile_of(self.member_a).profession, 'Data Scientist')


class SeedTemplateTests(TwoCompanyTestCase):
    def test_seeding_is_idempotent_and_sector_aware(self):
        from django.core.management import call_command

        call_command('seed_default_skills', verbosity=0)
        first = (DefaultProfession.objects.count(), DefaultSkill.objects.count())
        self.assertGreater(first[0], 0)
        self.assertGreater(first[1], 0)
        call_command('seed_default_skills', verbosity=0)
        self.assertEqual((DefaultProfession.objects.count(), DefaultSkill.objects.count()), first)
        # Sector-specific rows only attach where the sector exists. The
        # fixture's sector is 'Software', not 'Software & Technology', so
        # every seeded row here is a global one.
        self.assertFalse(DefaultSkill.objects.filter(sector__isnull=False).exists())
