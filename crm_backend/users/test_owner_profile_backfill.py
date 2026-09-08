"""Every company owner holds a membership row, like every other member.

Companies created before `register_company_in_transaction` started writing an
Owner-role `CompanyUserProfile` had their owner exist as `Company.owner` and
nowhere else. Every feature that built a list of people from
`CompanyUserProfile` therefore omitted the one person who could not be omitted,
and each grew its own "unless it's the owner" branch to compensate: a
synthesized roster row, a +1 on the member count, a notification preference
that defaulted rather than existed.

Users migration 0008 backfills the missing rows. These tests cover the
backfill against a legacy fixture, and the consequences of the branches being
gone.
"""

from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from company.models import Company
from django.apps import apps as django_apps
from django.utils import timezone

from users.models import CompanyUserProfile, User

PASSWORD = 'Kx9#mQ2vLp8Z'


class OwnerProfileBackfillTests(TwoCompanyTestCase):
    """The backfill itself, run against the legacy shape.

    The data function is invoked directly against the live app registry rather
    than by winding the migration graph backwards. Rewinding would mutate the
    schema of whichever xdist worker happened to run this, and the thing worth
    testing is the logic, not Django's ability to apply a RunPython.

    The legacy shape is reproduced exactly: a company whose owner has no
    CompanyUserProfile row.
    """

    def setUp(self):
        super().setUp()
        from importlib import import_module

        self.forwards = import_module(
            'users.migrations.0008_backfill_owner_company_profiles'
        ).forwards

    def run_backfill(self):
        self.forwards(django_apps, None)

    def make_legacy_company(self, name='Legacy Co'):
        """A company whose owner has no membership row -- what every company
        registered before register_company_in_transaction looked like."""
        owner = User.objects.create_user(
            email=f'{name.lower().replace(" ", "-")}@example.com',
            username=name.lower().replace(' ', '-'), password=PASSWORD,
        )
        company = Company.objects.create(name=name, owner=owner, sector=self.company_a.sector)
        self.assertFalse(CompanyUserProfile.objects.filter(company=company, user=owner).exists())
        return company, owner

    def test_a_legacy_owner_gets_an_owner_role_profile(self):
        company, owner = self.make_legacy_company()
        self.run_backfill()
        profile = CompanyUserProfile.objects.get(company=company, user=owner)
        self.assertEqual(profile.role, CompanyUserProfile.Role.Owner)
        self.assertTrue(profile.is_active)

    def test_an_existing_profile_is_never_edited(self):
        """The migration adds. An owner who already holds a row -- including
        one at an unexpected role, which transfer of ownership can produce --
        is left exactly as they are, because changing somebody's role is a
        decision and not a data fix."""
        profile = CompanyUserProfile.objects.get(user=self.owner_a, company=self.company_a)
        profile.role = CompanyUserProfile.Role.COMPANY_MANAGER
        profile.phone_number = '555-0100'
        profile.save(update_fields=['role', 'phone_number'])

        self.run_backfill()

        profile.refresh_from_db()
        self.assertEqual(profile.role, CompanyUserProfile.Role.COMPANY_MANAGER)
        self.assertEqual(profile.phone_number, '555-0100')
        self.assertEqual(
            CompanyUserProfile.objects.filter(user=self.owner_a, company=self.company_a).count(), 1,
        )

    def test_running_it_twice_creates_nothing_extra(self):
        """Migrations run once, but a data migration that is not idempotent is
        a trap for anyone replaying one after a partial deploy."""
        company, owner = self.make_legacy_company()
        self.run_backfill()
        after_first = CompanyUserProfile.objects.filter(company=company, user=owner).count()
        self.run_backfill()
        after_second = CompanyUserProfile.objects.filter(company=company, user=owner).count()
        self.assertEqual((after_first, after_second), (1, 1))

    def test_it_touches_nobody_who_is_not_an_owner(self):
        before = CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a)
        self.run_backfill()
        after = CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a)
        self.assertEqual(after.role, before.role)
        self.assertEqual(after.department_id, before.department_id)


class OwnerIsAMemberLikeAnyoneElseTests(TwoCompanyTestCase):
    """What the removed branches used to paper over."""

    def test_the_owner_appears_on_the_member_roster(self):
        response = self.client.get('/api/v1/analytics/company/members/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200, response.content)
        members = response.json()['data']['members']
        self.assertIn(str(self.owner_a.id), {member['id'] for member in members})

    def test_the_owner_can_read_and_update_their_own_profile(self):
        response = self.client.patch(
            '/api/v1/company/members/me/profile/',
            '{"profession": "Founder"}', content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        profile = CompanyUserProfile.objects.get(user=self.owner_a, company=self.company_a)
        self.assertEqual(profile.profession, 'Founder')

    def test_the_owner_has_a_real_notification_preference(self):
        """It used to default to enabled because there was no row to read.
        Now it is a stored value the owner can actually change."""
        response = self.client.patch(
            '/api/v1/company/members/me/notification-preference/',
            '{"email_notifications_enabled": false}', content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        profile = CompanyUserProfile.objects.get(user=self.owner_a, company=self.company_a)
        self.assertFalse(profile.email_notifications_enabled)

    def test_the_owner_is_counted_once_not_twice(self):
        """The old member count added +1 for an owner with no profile. With a
        real row and the +1 still in place it would have double-counted."""
        from analytics import services as analytics_services

        stats = async_to_sync(analytics_services.get_company_stats)(self.company_a)
        self.assertEqual(
            stats['member_count'],
            CompanyUserProfile.objects.filter(company=self.company_a).count(),
        )

    def test_the_owner_appears_exactly_once_in_the_workload_roster(self):
        from analytics import services as analytics_services

        roster = async_to_sync(analytics_services.get_company_workload)(self.company_a)
        owner_rows = [row for row in roster if row['id'] == str(self.owner_a.id)]
        self.assertEqual(len(owner_rows), 1)

    def test_the_owner_is_still_listed_first(self):
        from analytics import services as analytics_services

        roster = async_to_sync(analytics_services.get_company_workload)(self.company_a)
        self.assertEqual(roster[0]['id'], str(self.owner_a.id))

    def test_the_owner_is_in_the_assignable_pool(self):
        from projects_and_tasks import services as project_services
        from projects_and_tasks.models import Project

        project = Project.objects.create(
            title='Website Revamp', company=self.company_a, start_date=timezone.now(),
            deadline=timezone.now() + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )
        candidates, error = async_to_sync(project_services.list_eligible_assignees)(self.owner_a, project)
        self.assertIsNone(error)
        self.assertIn(self.owner_a.id, {user.id for user in candidates})
