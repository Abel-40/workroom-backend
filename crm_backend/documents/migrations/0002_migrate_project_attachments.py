"""Move project documents off Attachment and onto Document(scope="project").

`Attachment` served two jobs that look alike and are not:

- **Project and task documents** -- a file somebody filed against a project.
  That is what `Document` is for, and those rows move here.
- **Task-approval evidence** (`approval` is set) -- a file submitted for
  judgement inside one review cycle, part of an append-only history that
  PRESERVE EXACTLY names. Those rows **stay** on `Attachment`. A document is
  something you filed; evidence is something you submitted. Merging them would
  have meant either evidence inheriting document deletion, or documents
  inheriting evidence's immutability.

Link and page attachments stay too: they hold no file, and `Document` is a
file store. A URL pinned to a project is a different feature from a document,
and giving it a `FileField` row with nothing in it would be pretending
otherwise.

Additive, per the no-destructive-migrations rule. Nothing is deleted from
`Attachment`; the copied rows are simply no longer what the API reads. The
table is dropped in a later release once nothing references it, the same
treatment `Project.collaborators` and `ProjectVisibilityRequest` are getting.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def forwards(apps, schema_editor):
    Attachment = apps.get_model('projects_and_tasks', 'Attachment')
    Document = apps.get_model('documents', 'Document')

    # Only file attachments that are not evidence, on projects that still
    # exist. `is_deleted` is carried across rather than filtered out: a
    # soft-deleted document is still recoverable, and dropping those here
    # would quietly make somebody's restore impossible.
    candidates = Attachment.objects.filter(
        type='file', approval__isnull=True,
    ).select_related('project')

    already = set(Document.objects.values_list('legacy_attachment_id', flat=True))
    to_create = []
    skipped = 0
    for attachment in candidates.iterator():
        if attachment.id in already:
            continue
        if attachment.project_id is None or not attachment.file:
            skipped += 1
            continue
        to_create.append(Document(
            company_id=attachment.project.company_id,
            scope='project',
            project_id=attachment.project_id,
            task_id=attachment.task_id,
            file=attachment.file,
            name=attachment.name or 'Untitled',
            label=attachment.label or '',
            content_type=attachment.content_type or '',
            size=attachment.size or 0,
            uploaded_by_id=attachment.uploaded_by_id,
            is_deleted=attachment.is_deleted,
            # No deleted_at to carry over -- Attachment never recorded one.
            # Leaving it null means the purge job will not treat these as
            # expired, which is the safe direction: they wait for someone to
            # delete them again rather than disappearing on a clock that was
            # never running.
            deleted_at=None,
            legacy_attachment_id=attachment.id,
        ))

    if to_create:
        Document.objects.bulk_create(to_create, batch_size=500)
        logger.info('documents.migrated_from_attachment count=%d', len(to_create))
    if skipped:
        logger.warning(
            'documents.migration_skipped count=%d -- attachments with no project or no stored file',
            skipped,
        )


def backwards(apps, schema_editor):
    """Remove only the rows this created. The Attachment rows they came from
    were never deleted, so this restores the previous state exactly."""
    Document = apps.get_model('documents', 'Document')
    Document.objects.filter(legacy_attachment_id__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0001_initial'),
        ('projects_and_tasks', '0017_taskdependency'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
