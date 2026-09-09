"""Plan limits: resolving, counting, enforcing, reconciling.

The six scenarios §11 asks for are each a class below. The one that matters
most in the long run is `PlanSnapshotIsolationTests`: editing a plan's numbers
must never change what an existing customer is already paying for, and that is
a property no amount of care at the call sites can provide -- it has to come
from the data model.
"""

from datetime import timedelta
from unittest.mock import patch

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from django.test import override_settings
from django.utils import timezone
from documents.models import Document
from plans.models import Plan
from projects_and_tasks.models import Project
from subscriptions.models import Subscription
from users.models import CompanyUserProfile, User

from entitlements import services
from entitlements.models import UsageCounter
from entitlements.tasks import reconcile_usage_counters_task

PASSWORD = 'Kx9#mQ2vLp8Z'
M = UsageCounter.Metric


class EntitlementWorldMixin:
    def setUp(self):
        super().setUp()
        self.free = Plan.objects.get(key=Plan.Key.FREE)
        self.business = Plan.objects.get(key=Plan.Key.BUSINESS)

    def subscribe(self, plan, *, company=None, status='active', snapshot=True, past_due_since=None):
        company = company or self.company_a
        Subscription.objects.filter(company=company).delete()
        return Subscription.objects.create(
            company=company, plan=plan, status=status,
            plan_snapshot=plan.as_snapshot() if snapshot else {},
            past_due_since=past_due_since,
        )

    def check(self, metric, requested=1, company=None):
        return async_to_sync(services.check)(company or self.company_a, metric, requested)

    def make_projects(self, count, *, status=Project.STATUS.ACTIVE, company=None):
        company = company or self.company_a
        for index in range(count):
            Project.objects.create(
                title=f'Project {index}', company=company, status=status,
                start_date=timezone.now(), deadline=timezone.now() + timedelta(days=30),
                created_by=self.owner_a,
            )


class SeededPlanTests(EntitlementWorldMixin, TwoCompanyTestCase):
    """The four plans, with §11's numbers. These are asserted rather than
    trusted because the table is the contract."""

    def test_all_four_plans_exist(self):
        self.assertEqual(Plan.objects.filter(key__isnull=False).count(), 4)

    def test_free_limits(self):
        self.assertEqual(self.free.max_members, 5)
        self.assertEqual(self.free.max_active_projects, 3)
        self.assertEqual(self.free.max_departments_limit, 1)
        self.assertEqual(self.free.max_teams, 0)
        self.assertEqual(self.free.storage_bytes, 2 * 1024 ** 3)
        self.assertEqual(self.free.info_portal_page_limit, 50)
        self.assertEqual(self.free.ai_credits_per_month, 50)
        self.assertEqual(self.free.features, [])

    def test_free_disables_planning_rather_than_leaving_it_unlimited(self):
        """0 and null are different here, and confusing them would give the
        free tier unlimited planning -- the most expensive AI call there is."""
        self.assertEqual(self.free.ai_planning_generations_per_month, 0)
        self.assertIsNone(self.business.ai_planning_generations_per_month)

    def test_business_features_include_the_team_ones(self):
        for feature in ('workload_policy_warn', 'skills_matching', 'public_projects'):
            self.assertIn(feature, self.business.features)
        for feature in ('workload_policy_block', 'company_analytics', 'audit_log_export'):
            self.assertIn(feature, self.business.features)

    def test_enterprise_reserves_the_sso_key(self):
        enterprise = Plan.objects.get(key=Plan.Key.ENTERPRISE)
        self.assertIn('sso', enterprise.features)


class LimitBoundaryTests(EntitlementWorldMixin, TwoCompanyTestCase):
    """§11's table-driven case: at the limit is allowed, one past is blocked,
    null is always allowed."""

    def setUp(self):
        super().setUp()
        self.subscribe(self.free)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_exactly_at_the_limit_is_allowed(self):
        self.make_projects(2)  # limit 3, adding 1 -> 3
        self.assertTrue(self.check(M.ACTIVE_PROJECTS).allowed)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_one_past_the_limit_is_blocked(self):
        self.make_projects(3)
        result = self.check(M.ACTIVE_PROJECTS)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, 'limit_reached')
        self.assertEqual(result.current, 3)
        self.assertEqual(result.limit, 3)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_unlimited_is_always_allowed(self):
        self.subscribe(self.business)
        self.make_projects(50)
        result = self.check(M.ACTIVE_PROJECTS)
        self.assertTrue(result.allowed)
        self.assertIsNone(result.limit)
        self.assertIsNone(result.remaining)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_archived_and_done_projects_do_not_count(self):
        """§11: only Active status counts. Otherwise a company that finishes
        its work is punished for having done it."""
        self.make_projects(5, status=Project.STATUS.DONE)
        self.assertTrue(self.check(M.ACTIVE_PROJECTS).allowed)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_a_larger_request_is_measured_whole(self):
        """Storage asks for the file's size, not for one."""
        result = self.check(M.STORAGE_BYTES, requested=3 * 1024 ** 3)
        self.assertFalse(result.allowed)

    def test_a_company_with_no_subscription_gets_the_free_plan(self):
        """Every company registered before billing existed is in this state.
        Treating it as unlimited would apply the limits to paying customers
        and nobody else."""
        Subscription.objects.filter(company=self.company_a).delete()
        entitlements = async_to_sync(services.resolve)(self.company_a)
        self.assertEqual(entitlements.plan_key, 'free')


class EnforcementSwitchTests(EntitlementWorldMixin, TwoCompanyTestCase):
    """The flag that resolves R1. Whichever way it is set, the numbers
    reported are the real ones."""

    def setUp(self):
        super().setUp()
        self.subscribe(self.free)
        self.make_projects(5)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_enforced_refuses(self):
        self.assertFalse(self.check(M.ACTIVE_PROJECTS).allowed)

    @override_settings(ENTITLEMENTS_ENFORCED=False)
    def test_permissive_allows(self):
        self.assertTrue(self.check(M.ACTIVE_PROJECTS).allowed)

    @override_settings(ENTITLEMENTS_ENFORCED=False)
    def test_permissive_still_reports_the_true_numbers(self):
        """So the UI can show "5 of 3 used" and an upgrade prompt, and so
        turning enforcement on later needs no caller to change."""
        result = self.check(M.ACTIVE_PROJECTS)
        self.assertEqual(result.current, 5)
        self.assertEqual(result.limit, 3)
        self.assertEqual(result.remaining, 0)


class DowngradeTests(EntitlementWorldMixin, TwoCompanyTestCase):
    """§11: a company that downgrades over the limit keeps everyone. Nothing
    is removed, nothing is archived; only the next creation is blocked."""

    def setUp(self):
        super().setUp()
        for index in range(6):
            user = User.objects.create_user(
                email=f'extra{index}@example.com', username=f'extra{index}', password=PASSWORD,
            )
            CompanyUserProfile.objects.create(
                user=user, company=self.company_a, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
            )

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_existing_members_are_kept_when_over_the_limit(self):
        before = CompanyUserProfile.objects.filter(company=self.company_a).count()
        self.subscribe(self.free)
        self.check(M.MEMBERS)
        self.assertEqual(CompanyUserProfile.objects.filter(company=self.company_a).count(), before)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_the_next_one_is_blocked(self):
        self.subscribe(self.free)
        result = self.check(M.MEMBERS)
        self.assertFalse(result.allowed)
        self.assertGreater(result.current, result.limit)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_nothing_is_deleted_by_resolving_a_limit(self):
        self.subscribe(self.free)
        projects_before = Project.objects.count()
        self.check(M.ACTIVE_PROJECTS)
        self.assertEqual(Project.objects.count(), projects_before)


class GracePeriodTests(EntitlementWorldMixin, TwoCompanyTestCase):
    """past_due keeps full access for 14 days, then goes read-only for new
    creation only. Existing data stays readable throughout, and nothing is
    ever deleted for non-payment."""

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_inside_the_grace_period_everything_still_works(self):
        self.subscribe(
            self.business, status='past_due', past_due_since=timezone.now() - timedelta(days=13),
        )
        self.assertTrue(self.check(M.ACTIVE_PROJECTS).allowed)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_past_the_grace_period_new_creation_is_blocked(self):
        self.subscribe(
            self.business, status='past_due', past_due_since=timezone.now() - timedelta(days=15),
        )
        result = self.check(M.ACTIVE_PROJECTS)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, 'past_due_grace_expired')

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_existing_data_remains_readable(self):
        self.make_projects(2)
        self.subscribe(
            self.business, status='past_due', past_due_since=timezone.now() - timedelta(days=30),
        )
        response = self.client.get('/api/v1/projects/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Project.objects.filter(company=self.company_a).count(), 2)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_past_due_with_no_recorded_start_is_treated_as_in_grace(self):
        """Guessing against the customer on missing data is how a billing bug
        becomes a support incident."""
        self.subscribe(self.business, status='past_due', past_due_since=None)
        self.assertTrue(self.check(M.ACTIVE_PROJECTS).allowed)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_an_active_subscription_is_never_read_only(self):
        self.subscribe(self.business, status='active')
        self.assertFalse(async_to_sync(services.resolve)(self.company_a).read_only)


class PlanSnapshotIsolationTests(EntitlementWorldMixin, TwoCompanyTestCase):
    """Changing a live Plan must not change what an existing subscriber has.

    The property the snapshot exists for, and one no amount of care at the call
    sites could provide.
    """

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_editing_the_live_plan_does_not_move_an_existing_subscribers_limit(self):
        self.subscribe(self.free)
        self.free.max_active_projects = 99
        self.free.save(update_fields=['max_active_projects'])

        self.assertEqual(self.check(M.ACTIVE_PROJECTS).limit, 3)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_editing_the_live_plan_does_not_grant_a_feature(self):
        self.subscribe(self.free)
        self.free.features = ['company_analytics']
        self.free.save(update_fields=['features'])

        self.assertFalse(async_to_sync(services.has_feature)(self.company_a, 'company_analytics'))

    def test_a_subscription_with_no_snapshot_falls_back_to_the_live_plan(self):
        """Legacy subscriptions predate the field, and must still resolve."""
        self.subscribe(self.free, snapshot=False)
        self.assertEqual(async_to_sync(services.resolve)(self.company_a).plan_key, 'free')


class AiCreditTests(EntitlementWorldMixin, TwoCompanyTestCase):
    """Monthly metrics use the counter, because credits spent leave no other
    trace -- there is nothing to recompute them from."""

    def setUp(self):
        super().setUp()
        self.subscribe(self.free)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_credits_accumulate_and_then_block(self):
        async_to_sync(services.record_usage)(self.company_a, M.AI_CREDITS, 49)
        self.assertTrue(self.check(M.AI_CREDITS, services.COST_ASSISTANT_QUESTION).allowed)
        async_to_sync(services.record_usage)(self.company_a, M.AI_CREDITS, 1)
        self.assertFalse(self.check(M.AI_CREDITS, services.COST_ASSISTANT_QUESTION).allowed)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_last_months_spend_does_not_count_against_this_month(self):
        """Monthly metrics reset by period key -- a new month is a new row, so
        there is no job to fail at midnight on the first."""
        UsageCounter.objects.create(
            company=self.company_a, metric=M.AI_CREDITS, period='2020-01', value=9999,
        )
        self.assertTrue(self.check(M.AI_CREDITS, 1).allowed)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_free_cannot_run_a_planning_generation_at_all(self):
        """§11: the limit is 0, which is disabled rather than unlimited."""
        result = self.check(M.AI_PLANNING_GENERATIONS)
        self.assertFalse(result.allowed)
        self.assertEqual(result.limit, 0)

    def test_planning_cost_scales_with_brief_size_and_is_capped(self):
        self.assertEqual(services.planning_generation_cost(''), services.COST_PLANNING_MIN)
        self.assertEqual(services.planning_generation_cost('x' * 4000), services.COST_PLANNING_MIN + 2)
        self.assertEqual(services.planning_generation_cost('x' * 10_000_000), services.COST_PLANNING_MAX)

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_a_rejected_check_means_no_provider_call(self):
        """§11 asks for this explicitly: never pay for a call you were going
        to reject. The check happens before the provider, so a refusal costs
        nothing."""
        async_to_sync(services.record_usage)(self.company_a, M.AI_CREDITS, 50)
        project = Project.objects.create(
            title='P', company=self.company_a, start_date=timezone.now(),
            deadline=timezone.now() + timedelta(days=30), created_by=self.owner_a,
        )
        with patch('ai_agent.tasks_health.requests.post') as post:
            allowed = self.check(M.AI_CREDITS, services.COST_HEALTH_SUMMARY).allowed
            self.assertFalse(allowed)
            post.assert_not_called()
        self.assertIsNotNone(project.id)


class UsageCounterTests(EntitlementWorldMixin, TwoCompanyTestCase):
    def test_recording_is_atomic_and_returns_the_new_value(self):
        first = async_to_sync(services.record_usage)(self.company_a, M.AI_CREDITS, 3)
        second = async_to_sync(services.record_usage)(self.company_a, M.AI_CREDITS, 4)
        self.assertEqual((first, second), (3, 7))

    def test_a_counter_never_goes_negative(self):
        """A negative counter would quietly grant free capacity, which is
        worse than being briefly wrong the other way."""
        async_to_sync(services.record_usage)(self.company_a, M.MEMBERS, -5)
        self.assertEqual(services.usage_sync(self.company_a, M.MEMBERS), 0)

    def test_counters_are_scoped_per_company(self):
        async_to_sync(services.record_usage)(self.company_a, M.AI_CREDITS, 10)
        self.assertEqual(services.usage_sync(self.company_b, M.AI_CREDITS), 0)


class ReconciliationTests(EntitlementWorldMixin, TwoCompanyTestCase):
    """§11: desync a counter, run the job, confirm it is corrected and logged."""

    def test_a_drifted_counter_is_corrected(self):
        self.make_projects(2)
        UsageCounter.objects.update_or_create(
            company=self.company_a, metric=M.ACTIVE_PROJECTS, period='', defaults={'value': 99},
        )
        reconcile_usage_counters_task()
        self.assertEqual(services.usage_sync(self.company_a, M.ACTIVE_PROJECTS), 2)

    def test_the_correction_is_logged(self):
        self.make_projects(1)
        UsageCounter.objects.update_or_create(
            company=self.company_a, metric=M.ACTIVE_PROJECTS, period='', defaults={'value': 42},
        )
        with self.assertLogs('entitlements.tasks', level='WARNING') as logs:
            reconcile_usage_counters_task()
        self.assertTrue(any('drift_corrected' in line for line in logs.output))

    def test_an_accurate_counter_is_left_alone(self):
        """Scoped to the one metric: the fixture's other counters start
        unset, so a whole-run correction count would be non-zero for reasons
        that have nothing to do with this."""
        self.make_projects(2)
        UsageCounter.objects.update_or_create(
            company=self.company_a, metric=M.ACTIVE_PROJECTS, period='', defaults={'value': 2},
        )
        corrections = services.reconcile_company_sync(self.company_a)
        self.assertNotIn(M.ACTIVE_PROJECTS, [c['metric'] for c in corrections])

    def test_monthly_counters_are_never_reconciled(self):
        """There is nothing to recompute them from -- spending leaves no other
        trace, so the counter is the record rather than a cache of one."""
        async_to_sync(services.record_usage)(self.company_a, M.AI_CREDITS, 7)
        reconcile_usage_counters_task()
        self.assertEqual(services.usage_sync(self.company_a, M.AI_CREDITS), 7)

    def test_a_drifted_counter_cannot_wrongly_refuse(self):
        """The decision path reads rows, not the counter, so drift can make a
        dashboard wrong but never a limit."""
        self.subscribe(self.free)
        UsageCounter.objects.update_or_create(
            company=self.company_a, metric=M.ACTIVE_PROJECTS, period='', defaults={'value': 999},
        )
        with override_settings(ENTITLEMENTS_ENFORCED=True):
            self.assertTrue(self.check(M.ACTIVE_PROJECTS).allowed)


class FeatureGateTests(EntitlementWorldMixin, TwoCompanyTestCase):
    """Enforced throughout, because that is what these test. With
    enforcement off every feature answers True by design -- the switch means
    "do plan restrictions apply at all", not "do count limits apply"."""

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_free_has_no_features(self):
        self.subscribe(self.free)
        for feature in ('public_projects', 'company_analytics', 'workload_policy_warn'):
            self.assertFalse(async_to_sync(services.has_feature)(self.company_a, feature))

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_business_has_them(self):
        self.subscribe(self.business)
        for feature in ('public_projects', 'company_analytics', 'workload_policy_block'):
            self.assertTrue(async_to_sync(services.has_feature)(self.company_a, feature))

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_company_analytics_needs_both_the_role_and_the_plan(self):
        """Being the Owner does not conjure a feature the company is not
        paying for, and paying for it does not hand it to a member."""
        from analytics.tiers import can_view_company_analytics

        self.subscribe(self.free)
        self.assertFalse(async_to_sync(can_view_company_analytics)(self.owner_a, self.company_a))

        self.subscribe(self.business)
        self.assertTrue(async_to_sync(can_view_company_analytics)(self.owner_a, self.company_a))
        self.assertFalse(async_to_sync(can_view_company_analytics)(self.member_a, self.company_a))

    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_personal_analytics_is_never_plan_gated(self):
        """§11 says so explicitly, and a product that hides your own workload
        behind an upsell is a worse product."""
        self.subscribe(self.free)
        response = self.client.get('/api/v1/analytics/me/', **auth_header(self.member_a))
        self.assertEqual(response.status_code, 200, response.content)


class StorageEnforcementTests(EntitlementWorldMixin, TwoCompanyTestCase):
    @override_settings(ENTITLEMENTS_ENFORCED=True)
    def test_an_upload_over_the_storage_limit_is_refused_with_an_upgrade_prompt(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.subscribe(self.free)
        # Fill the quota with a recorded document rather than real bytes.
        Document.objects.create(
            company=self.company_a, scope=Document.Scope.COMPANY, name='big.pdf',
            file='documents/big.pdf', size=2 * 1024 ** 3, uploaded_by=self.owner_a,
        )
        upload = SimpleUploadedFile('more.pdf', b'%PDF-1.4 more', content_type='application/pdf')
        response = self.client.post(
            '/api/v1/documents/', {'file': upload, 'scope': 'company'}, **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 402, response.content)
        self.assertIn('upgrade', response.json()['message'].lower())

    @override_settings(ENTITLEMENTS_ENFORCED=False)
    def test_permissive_mode_lets_it_through(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.subscribe(self.free)
        Document.objects.create(
            company=self.company_a, scope=Document.Scope.COMPANY, name='big.pdf',
            file='documents/big.pdf', size=2 * 1024 ** 3, uploaded_by=self.owner_a,
        )
        upload = SimpleUploadedFile('more.pdf', b'%PDF-1.4 more', content_type='application/pdf')
        response = self.client.post(
            '/api/v1/documents/', {'file': upload, 'scope': 'company'}, **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201, response.content)


class PermissiveByDefaultTests(EntitlementWorldMixin, TwoCompanyTestCase):
    """The V1 default, pinned so that turning enforcement on is a deliberate
    act rather than something that arrives with a settings tidy-up."""

    def test_enforcement_is_off_by_default(self):
        from django.conf import settings

        self.assertFalse(settings.ENTITLEMENTS_ENFORCED)

    def test_a_free_company_can_still_exceed_every_limit(self):
        self.subscribe(self.free)
        self.make_projects(20)
        self.assertTrue(self.check(M.ACTIVE_PROJECTS).allowed)

    def test_features_are_all_available(self):
        """The switch means "do plan restrictions apply at all". Permissive on
        counts but restrictive on features would be the worst of both."""
        self.subscribe(self.free)
        self.assertTrue(async_to_sync(services.has_feature)(self.company_a, 'company_analytics'))

    def test_usage_is_still_counted_and_reported(self):
        """The part that cannot be reconstructed retroactively. Whatever is
        decided about enforcement, the numbers to decide it with exist."""
        self.subscribe(self.free)
        self.make_projects(7)
        result = self.check(M.ACTIVE_PROJECTS)
        self.assertTrue(result.allowed)
        self.assertEqual(result.current, 7)
        self.assertEqual(result.limit, 3)
