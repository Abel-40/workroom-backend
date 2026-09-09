"""Backfill ProjectMembership from the state the access model used to imply.

Two grants existed only as implications before this, and both have to become
explicit rows or people lose access the moment the new resolver ships:

``collaborators`` -> ``contributor``
    The M2M is deprecated but still dual-written for one release. These become
    contributor memberships, which is a slight *widening*: a collaborator used
    to get VIEW on a private project only, and now gets CONTRIBUTE on any
    visibility. That is the intended meaning of the field -- people were put
    there to work on the project, not to read it.

``created_by`` -> ``manager``
    ``created_by`` used to grant MANAGE and now grants VIEW only, as permanent
    provenance. Without this half of the backfill, every existing project run
    by the person who created it would lose its manager overnight. The row
    makes the grant explicit and, unlike ``created_by``, revocable.

Idempotent: re-running creates nothing new, so it is safe to re-apply and safe
to run against a database where some rows already exist.

Reversible in the schema sense only -- the reverse drops the rows this created,
which is correct for a rollback because the previous release still reads
``collaborators`` and ``created_by`` directly.
"""

from django.db import migrations

CONTRIBUTOR = 'contributor'
MANAGER = 'manager'


def backfill(apps, schema_editor):
    Project = apps.get_model('projects_and_tasks', 'Project')
    ProjectMembership = apps.get_model('projects_and_tasks', 'ProjectMembership')

    existing = set(ProjectMembership.objects.values_list('project_id', 'user_id'))
    to_create = []

    # The creator becomes an explicit manager. Skipped when they are already
    # the current_owner, which grants MANAGE on its own and needs no row, and
    # when created_by is NULL (the user was deleted).
    for project_id, created_by_id, current_owner_id in Project.objects.exclude(
        created_by__isnull=True,
    ).values_list('id', 'created_by_id', 'current_owner_id'):
        if created_by_id == current_owner_id:
            continue
        if (project_id, created_by_id) in existing:
            continue
        existing.add((project_id, created_by_id))
        to_create.append(ProjectMembership(project_id=project_id, user_id=created_by_id, role=MANAGER))

    through = Project.collaborators.through
    for project_id, user_id in through.objects.values_list('project_id', 'user_id'):
        if (project_id, user_id) in existing:
            continue
        existing.add((project_id, user_id))
        to_create.append(ProjectMembership(project_id=project_id, user_id=user_id, role=CONTRIBUTOR))

    ProjectMembership.objects.bulk_create(to_create, batch_size=1000)


def unbackfill(apps, schema_editor):
    """Drop only what the backfill could have created. A membership with an
    ``added_by`` was granted by a person through the members panel and is not
    this migration's to remove."""
    ProjectMembership = apps.get_model('projects_and_tasks', 'ProjectMembership')
    ProjectMembership.objects.filter(added_by__isnull=True, role__in=(CONTRIBUTOR, MANAGER)).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('projects_and_tasks', '0010_projectmembership'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
