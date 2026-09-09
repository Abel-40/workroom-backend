"""Integration seams. Models only -- §12 asks for the shape, not the behaviour.

There is nothing to test about connecting, because nothing connects. What is
worth pinning is the *shape*: the constraints that make the two stated rules
enforceable rather than aspirational, and the absence of any code that acts on
external data.
"""

from api.tests import TwoCompanyTestCase
from django.db import IntegrityError, transaction
from users.models import User

from integrations.models import (
    ExternalIdentityLink,
    ExternalObjectLink,
    Integration,
    IntegrationCredential,
    IntegrationProvider,
    WebhookEvent,
)

PASSWORD = 'Kx9#mQ2vLp8Z'


class IntegrationSeamMixin:
    def setUp(self):
        super().setUp()
        self.provider = IntegrationProvider.objects.create(
            key='github', name='GitHub', kind=IntegrationProvider.Kind.SOURCE_CONTROL,
            available_scopes=['repo:read', 'pr:read'],
        )

    def connect(self, company=None, scopes=None):
        return Integration.objects.create(
            company=company or self.company_a, provider=self.provider,
            connected_by=self.owner_a, scopes=scopes or ['repo:read'],
            status=Integration.Status.ACTIVE, is_active=True,
        )


class IntegrationShapeTests(IntegrationSeamMixin, TwoCompanyTestCase):
    def test_one_connection_per_provider_per_company(self):
        """Two would make "which credential does this link use" unanswerable."""
        self.connect()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.connect()

    def test_two_companies_may_each_connect_the_same_provider(self):
        self.connect()
        self.connect(company=self.company_b)
        self.assertEqual(Integration.objects.count(), 2)

    def test_an_integration_carries_its_own_scopes(self):
        """A service principal, not a proxy for the person who connected it.
        The scopes are stored, not derived from the connector's role."""
        integration = self.connect(scopes=['repo:read'])
        self.assertEqual(integration.scopes, ['repo:read'])

    def test_it_survives_the_connecting_user_leaving(self):
        """SET_NULL provenance, the same reasoning as Project.created_by. An
        integration whose access followed one person's employment would break
        the moment they left -- or worse, quietly keep their rights."""
        connector = User.objects.create_user(
            email='connector@example.com', username='connector', password=PASSWORD,
        )
        integration = Integration.objects.create(
            company=self.company_a, provider=self.provider, connected_by=connector,
        )
        connector.delete()
        integration.refresh_from_db()
        self.assertIsNone(integration.connected_by_id)
        self.assertTrue(Integration.objects.filter(id=integration.id).exists())

    def test_credentials_live_in_their_own_table(self):
        """The row that must never be serialized, logged or returned. Keeping
        it separate makes that boundary visible rather than remembered."""
        integration = self.connect()
        credential = IntegrationCredential.objects.create(integration=integration)
        self.assertEqual(credential.encrypted_access_token, '')


class ExternalIdentityTests(IntegrationSeamMixin, TwoCompanyTestCase):
    """Per company, never carried across -- which is the whole reason this is
    not a field on User."""

    def test_the_same_person_links_separately_per_company(self):
        for company in (self.company_a, self.company_b):
            ExternalIdentityLink.objects.create(
                company=company, user=self.owner_a, provider=self.provider,
                external_user_id='gh-1', external_username='octocat',
            )
        self.assertEqual(ExternalIdentityLink.objects.filter(user=self.owner_a).count(), 2)

    def test_one_link_per_user_per_provider_within_a_company(self):
        ExternalIdentityLink.objects.create(
            company=self.company_a, user=self.owner_a, provider=self.provider, external_user_id='gh-1',
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExternalIdentityLink.objects.create(
                company=self.company_a, user=self.owner_a, provider=self.provider, external_user_id='gh-2',
            )

    def test_two_people_cannot_claim_the_same_external_account(self):
        """The reverse direction. Without it, any future matching becomes
        ambiguous at exactly the wrong moment."""
        ExternalIdentityLink.objects.create(
            company=self.company_a, user=self.owner_a, provider=self.provider, external_user_id='gh-1',
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExternalIdentityLink.objects.create(
                company=self.company_a, user=self.member_a, provider=self.provider, external_user_id='gh-1',
            )

    def test_a_new_link_is_unverified(self):
        """A claimed external identity is a claim until the provider confirms
        it. Nothing may match on an unverified link."""
        link = ExternalIdentityLink.objects.create(
            company=self.company_a, user=self.owner_a, provider=self.provider, external_user_id='gh-1',
        )
        self.assertFalse(link.is_verified)
        self.assertIsNone(link.verified_at)


class ExternalObjectLinkTests(IntegrationSeamMixin, TwoCompanyTestCase):
    """External data is evidence, never truth."""

    def test_a_link_is_recorded_against_a_loose_target(self):
        integration = self.connect()
        link = ExternalObjectLink.objects.create(
            company=self.company_a, integration=integration,
            target_type='projects_and_tasks.task', target_id=self.owner_a.id,
            external_type='pull_request', external_id='42',
            external_url='https://example.com/pr/42', external_state='merged',
        )
        self.assertEqual(link.external_state, 'merged')

    def test_the_same_pair_cannot_be_linked_twice(self):
        integration = self.connect()
        fields = dict(
            company=self.company_a, integration=integration,
            target_type='projects_and_tasks.task', target_id=self.owner_a.id,
            external_type='pull_request', external_id='42',
        )
        ExternalObjectLink.objects.create(**fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExternalObjectLink.objects.create(**fields)

    def test_nothing_reads_an_external_link_to_change_workroom_state(self):
        """The rule stated as a test. If somebody later wires a provider that
        auto-completes a task from a merged PR, this fails and they have to
        argue for it deliberately."""
        import subprocess

        hits = subprocess.run(
            ['git', 'grep', '-l', 'ExternalObjectLink', '--', '*.py'],
            capture_output=True, text=True,
        ).stdout.split()
        # Only the models, this test, and migrations may mention it.
        allowed = {'integrations/models.py', 'integrations/tests.py'}
        unexpected = [
            path for path in hits
            if not path.endswith(tuple(allowed)) and '/migrations/' not in path
        ]
        self.assertEqual(unexpected, [], f'{unexpected} reads external links; see the module docstring')


class WebhookEventTests(IntegrationSeamMixin, TwoCompanyTestCase):
    def test_the_same_provider_event_cannot_be_stored_twice(self):
        """The idempotency guarantee, in the database rather than in a handler
        that has to remember. Providers retry, and a retry processed twice is
        how one merged pull request becomes two of something."""
        WebhookEvent.objects.create(provider=self.provider, external_event_id='evt-1')
        with self.assertRaises(IntegrityError), transaction.atomic():
            WebhookEvent.objects.create(provider=self.provider, external_event_id='evt-1')

    def test_two_providers_may_use_the_same_event_id(self):
        other = IntegrationProvider.objects.create(
            key='gitlab', name='GitLab', kind=IntegrationProvider.Kind.SOURCE_CONTROL,
        )
        WebhookEvent.objects.create(provider=self.provider, external_event_id='evt-1')
        WebhookEvent.objects.create(provider=other, external_event_id='evt-1')
        self.assertEqual(WebhookEvent.objects.count(), 2)

    def test_an_event_starts_received_rather_than_processed(self):
        """Stored first, acted on after -- so a crash mid-processing leaves a
        row to retry rather than an event nobody can prove arrived."""
        event = WebhookEvent.objects.create(provider=self.provider, external_event_id='evt-1')
        self.assertEqual(event.status, WebhookEvent.Status.RECEIVED)
        self.assertIsNone(event.processed_at)

    def test_a_duplicate_is_a_recordable_status_not_an_error(self):
        self.assertIn('duplicate', WebhookEvent.Status.values)


class NothingIsConnectedTests(IntegrationSeamMixin, TwoCompanyTestCase):
    """§12: connect nothing. No provider, no OAuth flow, no dispatcher."""

    def test_no_provider_is_enabled_by_default(self):
        provider = IntegrationProvider.objects.create(
            key='drive', name='Google Drive', kind=IntegrationProvider.Kind.STORAGE,
        )
        self.assertFalse(provider.is_enabled)

    def test_a_new_integration_is_pending_and_inactive(self):
        integration = Integration.objects.create(company=self.company_a, provider=self.provider)
        self.assertEqual(integration.status, Integration.Status.PENDING)
        self.assertFalse(integration.is_active)

    def test_there_are_no_integration_endpoints(self):
        """The seam is models only. An endpoint would be a connection."""
        response = self.client.get('/api/v1/integrations/')
        self.assertEqual(response.status_code, 404)
