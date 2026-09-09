"""Move ProjectVisibilityRequest rows onto the generic ApprovalRequest.

The decision says to delete the bespoke model and migrate its rows. The
non-negotiable rules say never to drop a populated table. Both are satisfied
by migrating the rows now, marking the old model deprecated in its docstring,
and removing the table in a later release once nothing reads it -- the same
treatment `Project.collaborators` is getting.

Also reports any project that is already `public`. Those are deliberately left
public: `Company.allow_public_projects` now defaults to off, but silently
unpublishing a project somebody is actively sharing would be a worse surprise
than leaving it as it was. The gate applies to *new* transitions. The rows are
logged at WARNING so whoever runs the deploy can review them and talk to the
companies concerned.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

KIND = 'project_visibility'
TARGET_TYPE = 'projects_and_tasks.project'
STATUS_MAP = {'pending': 'pending', 'approved': 'approved', 'denied': 'denied'}


def forwards(apps, schema_editor):
    ProjectVisibilityRequest = apps.get_model('projects_and_tasks', 'ProjectVisibilityRequest')
    ApprovalRequest = apps.get_model('projects_and_tasks', 'ApprovalRequest')
    Project = apps.get_model('projects_and_tasks', 'Project')

    already = set(
        ApprovalRequest.objects.filter(kind=KIND).values_list('target_id', 'created_at')
    )
    to_create = []
    for request in ProjectVisibilityRequest.objects.select_related('project'):
        if (request.project_id, request.created_at) in already:
            continue
        to_create.append(ApprovalRequest(
            company_id=request.project.company_id,
            kind=KIND,
            target_type=TARGET_TYPE,
            target_id=request.project_id,
            payload={'requested_visibility': request.requested_visibility},
            requested_by_id=request.requested_by_id,
            status=STATUS_MAP.get(request.status, 'denied'),
            decided_by_id=request.decided_by_id,
            decided_at=request.decided_at,
            decision_comment=request.decision_comment or '',
        ))
    # The pending-uniqueness constraint is per (kind, target). Historic data
    # could in principle hold two pending rows for one project if it predates
    # the old model's own constraint, so they are inserted one at a time and a
    # duplicate is downgraded rather than aborting the whole migration.
    for row in to_create:
        try:
            row.save()
        except Exception:
            logger.warning(
                'approval_request.backfill_conflict project=%s status=%s -- recorded as denied',
                row.target_id, row.status,
            )
            row.status = 'denied'
            row.save()

    public = list(
        Project.objects.filter(visibility='public', is_deleted=False)
        .values_list('id', 'title', 'company__name')
    )
    if public:
        logger.warning(
            'EXISTING PUBLIC PROJECTS -- REVIEW REQUIRED: %d project(s) are public and remain public. '
            'Company.allow_public_projects now defaults to off, so no *new* project can be published '
            'until an Owner switches it on. Review these with the companies concerned.',
            len(public),
        )
        for project_id, title, company_name in public:
            logger.warning('  public project %s "%s" (company: %s)', project_id, title, company_name)


def backwards(apps, schema_editor):
    ApprovalRequest = apps.get_model('projects_and_tasks', 'ApprovalRequest')
    ApprovalRequest.objects.filter(kind=KIND).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('projects_and_tasks', '0013_approvalrequest'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
