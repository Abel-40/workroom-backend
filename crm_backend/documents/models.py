"""One document model, four scopes.

Workroom was on its way to four separate document systems -- project
attachments existed, Info Portal folders existed, and personal and
company-wide files had nowhere to live at all. Four systems means four
permission rules, four upload validators, four delete semantics and four
places to forget the storage accounting.

This is one model with a ``scope``, and one function that decides who may
read it (:func:`documents.services.resolve_document_access`). Adding a fifth
scope means adding a branch there, not a fifth app.

What deliberately stays outside it: ``projects_and_tasks.Attachment`` rows
that carry an ``approval``. Those are task-approval *evidence*, not documents
-- they belong to one submission cycle, they are part of an append-only review
history, and PRESERVE EXACTLY names that flow. A document is a thing somebody
filed; evidence is a thing somebody submitted for judgement. Merging them
would have meant either evidence inheriting document deletion, or documents
inheriting evidence's immutability.
"""

from django.conf import settings
from django.db import models
from utils.models import UUIDModel


class Document(UUIDModel):
    """A stored file, scoped to whoever is meant to see it.

    ``scope`` decides which of the ownership columns is meaningful, and the
    constraints below enforce that the right one is set. A row that claims to
    be a project document without naming a project is not a state worth being
    able to represent.
    """

    class Scope(models.TextChoices):
        # The most private thing here, and the only scope with no
        # administrative override of any kind -- see resolve_document_access.
        PERSONAL = 'personal', 'Personal'
        PROJECT = 'project', 'Project'
        COMPANY = 'company', 'Company'
        FOLDER = 'folder', 'Info Portal folder'

    # Always set, whatever the scope. Storage accounting is per company, and a
    # document that cannot be attributed to one cannot be counted, retained or
    # purged against the right tenant.
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='documents')
    scope = models.CharField(max_length=20, choices=Scope.choices)

    # Exactly one of these is set, per the constraints below.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='personal_documents',
        null=True, blank=True,
    )
    project = models.ForeignKey(
        'projects_and_tasks.Project', on_delete=models.CASCADE, related_name='scoped_documents',
        null=True, blank=True,
    )
    folder = models.ForeignKey(
        'pages.PageFolder', on_delete=models.CASCADE, related_name='documents', null=True, blank=True,
    )
    # Optional narrowing within a project document. Not a scope of its own:
    # a task document is a project document that happens to name a task, and
    # it answers to the project's permissions either way.
    task = models.ForeignKey(
        'projects_and_tasks.Task', on_delete=models.SET_NULL, related_name='scoped_documents',
        null=True, blank=True,
    )

    file = models.FileField(upload_to='documents/')
    name = models.CharField(max_length=255)
    label = models.CharField(max_length=255, blank=True, default='')
    content_type = models.CharField(max_length=100, blank=True, default='')
    # Recorded at upload, never recomputed. Storage accounting reads this
    # rather than the storage backend, so a counter can be reconciled against
    # rows without talking to S3 for every file.
    size = models.PositiveBigIntegerField(default=0)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name='uploaded_documents', null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Soft delete with a retention window. `deleted_at` is the clock the purge
    # job reads -- a boolean alone cannot answer "how long ago", and without
    # that there is no window, only an ever-growing pile of hidden files.
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name='deleted_documents',
        null=True, blank=True,
    )

    # The Attachment row this was copied from, for rows that came across in
    # migration 0002. Null for anything uploaded since. Kept rather than
    # discarded because it makes the migration idempotent, and because it is
    # the only way to tell, later, which rows predate the unified model.
    legacy_attachment_id = models.UUIDField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # Each scope names its own owner and nothing else. Without these a
            # row could claim `personal` while pointing at a project, and
            # resolve_document_access would answer using a column the scope
            # says is meaningless.
            models.CheckConstraint(
                condition=(
                    models.Q(scope='personal', owner__isnull=False, project__isnull=True, folder__isnull=True)
                    | models.Q(scope='project', project__isnull=False, owner__isnull=True, folder__isnull=True)
                    | models.Q(scope='company', owner__isnull=True, project__isnull=True, folder__isnull=True)
                    | models.Q(scope='folder', folder__isnull=False, owner__isnull=True, project__isnull=True)
                ),
                name='document_scope_matches_its_owner',
            ),
        ]
        indexes = [
            # Storage accounting and the purge job: one company's live rows.
            models.Index(fields=['company', 'is_deleted']),
            # The purge job's own query: what is past its retention window.
            models.Index(fields=['is_deleted', 'deleted_at']),
            models.Index(fields=['scope', 'project']),
            models.Index(fields=['scope', 'owner']),
        ]

    def __str__(self):
        return f'{self.name} ({self.scope})'


class DocumentShare(UUIDModel):
    """Explicit access to a personal document.

    Personal documents have no administrative override -- not the company
    Owner, not a Company Manager, nobody. This row is the only way anyone
    other than the owner reads one, and it exists so that "private by default"
    does not have to mean "impossible to share".

    Deliberately not reused for the other scopes: a project document is
    already reachable by everyone with project VIEW, and a per-person grant on
    top of that would be a second, quieter permission system for the same
    thing.
    """

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='shares')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='shared_documents')
    shared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name='documents_shared', null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['document', 'user'], name='one_document_share_per_user'),
        ]
        indexes = [
            models.Index(fields=['user', 'document']),
        ]

    def __str__(self):
        return f'{self.document_id} shared with {self.user_id}'
