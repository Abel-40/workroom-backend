"""Integration seams. Models only -- nothing connects to anything.

§12 asks for the shape and explicitly not the behaviour: no provider, no OAuth
flow, no webhook dispatcher. What is here is the set of tables a real
integration would need, so that adding one later is a feature rather than a
schema migration across a live database.

Two rules matter enough to state on the models themselves, because they are
the ones that get quietly broken once someone is wiring up a real provider
under time pressure:

**An integration is a service principal with its own scopes.** It never
inherits the connecting user's rights. If the person who connected GitHub
leaves, or is demoted, the integration's access does not silently follow them
-- in either direction. That is why `Integration` carries its own `scopes` and
why `connected_by` is `SET_NULL` provenance rather than an owner.

**External data is evidence, never truth.** A linked pull request shows up as
evidence on a task approval; it never moves the task, never approves anything,
and never writes domain state. Workroom's database is authoritative and an
integration is a source of attachments and links, not a second writer.
"""

from django.conf import settings
from django.db import models
from utils.models import UUIDModel


class IntegrationProvider(UUIDModel):
    """A third-party system Workroom can link to.

    A row per provider rather than a Python enum, mirroring the existing
    AI-provider abstraction rather than inventing a second pattern: providers
    are configured, enabled and disabled by operators, and a code deploy is
    the wrong mechanism for that.
    """

    class Kind(models.TextChoices):
        SOURCE_CONTROL = 'source_control', 'Source control'
        STORAGE = 'storage', 'File storage'
        CALENDAR = 'calendar', 'Calendar'
        CHAT = 'chat', 'Chat'

    key = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=30, choices=Kind.choices)
    # What this provider *could* grant. An Integration's own scopes are a
    # subset of these, never a superset.
    available_scopes = models.JSONField(default=list, blank=True)
    is_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Integration(UUIDModel):
    """One company's connection to one provider.

    The service principal. Its `scopes` are its own -- not the connecting
    user's, and not recomputed from them. See the module docstring.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACTIVE = 'active', 'Active'
        # Distinct from disconnected: the credential stopped working and
        # somebody has to act, which is different from somebody choosing to
        # turn it off.
        ERROR = 'error', 'Error'
        DISCONNECTED = 'disconnected', 'Disconnected'

    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='integrations')
    provider = models.ForeignKey(IntegrationProvider, on_delete=models.PROTECT, related_name='integrations')
    # Provenance, not authority. SET_NULL so the integration survives the
    # person who set it up leaving -- the same reasoning as Project.created_by.
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='connected_integrations',
    )
    scopes = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    is_active = models.BooleanField(default=False)
    external_account_id = models.CharField(max_length=255, blank=True, default='')
    settings = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One connection per provider per company. Two would make "which
            # credential does this link use" unanswerable.
            models.UniqueConstraint(fields=['company', 'provider'], name='one_integration_per_company_provider'),
        ]
        indexes = [
            models.Index(fields=['company', 'is_active']),
        ]

    def __str__(self):
        return f'{self.company_id} -> {self.provider_id} ({self.status})'


class IntegrationCredential(UUIDModel):
    """Tokens for one integration.

    Separate from `Integration` on purpose: this is the row that must never be
    serialized, logged, or returned by an API, and keeping it in its own table
    makes that boundary something you can see rather than something you have to
    remember. Nothing in this codebase reads it yet.

    The token columns are named `encrypted_*` because that is what they must
    hold. There is no encryption helper here -- writing one before there is a
    provider to use it would be guessing at the key-management story, and a
    half-built one that looks finished is worse than an obviously empty seam.
    Whoever wires the first provider owns that decision, and the column names
    are there to stop anyone quietly storing plaintext in them.
    """

    integration = models.OneToOneField(Integration, on_delete=models.CASCADE, related_name='credential')
    encrypted_access_token = models.TextField(blank=True, default='')
    encrypted_refresh_token = models.TextField(blank=True, default='')
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'credential for {self.integration_id}'


class ExternalIdentityLink(UUIDModel):
    """A Workroom user's account on a provider, **per company**.

    Per company and never carried across, which is the whole reason this is
    not a field on `User`. Someone's GitHub account being linked at one
    employer is not a fact the next employer gets to inherit, and a global link
    would leak exactly that.

    `is_verified` starts false: a claimed external identity is a claim until
    the provider confirms it. Nothing may match on an unverified link.
    """

    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='external_identities')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='external_identities',
    )
    provider = models.ForeignKey(IntegrationProvider, on_delete=models.CASCADE, related_name='identity_links')
    external_user_id = models.CharField(max_length=255)
    external_username = models.CharField(max_length=255, blank=True, default='')
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'user', 'provider'], name='one_identity_link_per_company_user_provider',
            ),
            # The reverse direction too: one external account maps to at most
            # one Workroom user within a company. Without this, two people
            # could claim the same GitHub account and any matching becomes
            # ambiguous at exactly the wrong moment.
            models.UniqueConstraint(
                fields=['company', 'provider', 'external_user_id'],
                name='one_workroom_user_per_external_account',
            ),
        ]

    def __str__(self):
        return f'{self.user_id} = {self.external_username or self.external_user_id}'


class ExternalObjectLink(UUIDModel):
    """A link between a Workroom object and something in a provider.

    Loose reference on the Workroom side (`target_type`/`target_id`), matching
    AuditEvent and Notification: the target may be archived, and the link
    should still read.

    This is the evidence table. A row here says "this pull request is related
    to this task". It does not say the task is done, and nothing in Workroom
    may read it as if it did.
    """

    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='external_links')
    integration = models.ForeignKey(Integration, on_delete=models.CASCADE, related_name='object_links')
    target_type = models.CharField(max_length=64)
    target_id = models.UUIDField()
    external_type = models.CharField(max_length=64)
    external_id = models.CharField(max_length=255)
    external_url = models.URLField(blank=True, default='')
    title = models.CharField(max_length=255, blank=True, default='')
    # Last known state on the provider's side, as a label. Deliberately free
    # text rather than choices: it is *their* vocabulary, and normalising it
    # into ours would be the first step toward treating it as truth.
    external_state = models.CharField(max_length=64, blank=True, default='')
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='external_object_links',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['integration', 'target_type', 'target_id', 'external_type', 'external_id'],
                name='one_external_link_per_pair',
            ),
        ]
        indexes = [
            models.Index(fields=['target_type', 'target_id']),
        ]

    def __str__(self):
        return f'{self.target_type}:{self.target_id} -> {self.external_type}:{self.external_id}'


class WebhookEvent(UUIDModel):
    """One inbound event from a provider, recorded before it is acted on.

    Idempotent on the provider's own event id, which is the only identifier
    both sides agree about. Providers retry, and a retry that is processed
    twice is how one merged pull request becomes two comments -- or two of
    something that matters more.

    Stored first and processed after, so a crash mid-processing leaves a row
    to retry rather than an event nobody can prove arrived. Nothing dispatches
    these yet; §12 asks for the seam, not the dispatcher.
    """

    class Status(models.TextChoices):
        RECEIVED = 'received', 'Received'
        PROCESSED = 'processed', 'Processed'
        FAILED = 'failed', 'Failed'
        # Recorded rather than dropped: a duplicate arriving is a normal event
        # worth being able to see, not an error.
        DUPLICATE = 'duplicate', 'Duplicate'

    provider = models.ForeignKey(IntegrationProvider, on_delete=models.CASCADE, related_name='webhook_events')
    integration = models.ForeignKey(
        Integration, on_delete=models.SET_NULL, null=True, blank=True, related_name='webhook_events',
    )
    external_event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=100, blank=True, default='')
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)
    error_message = models.TextField(blank=True, default='')
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-received_at']
        constraints = [
            # The idempotency guarantee, in the database rather than in a
            # handler that has to remember to check.
            models.UniqueConstraint(
                fields=['provider', 'external_event_id'], name='one_webhook_event_per_provider_event_id',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'received_at']),
        ]

    def __str__(self):
        return f'{self.provider_id}:{self.external_event_id} ({self.status})'
