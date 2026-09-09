"""Give every company owner a CompanyUserProfile.

`register_company_in_transaction` has created an Owner-role profile alongside
the company for some time, so this only concerns companies that predate that.
For those, the owner existed as `Company.owner` and nowhere else, which meant
every place that built a list of people from `CompanyUserProfile` quietly
omitted the one person who could not be omitted: the member roster, the member
count, the assignable pool, and notification preferences.

Each of those grew its own "unless it's the owner" branch. This migration
removes the reason for them, and the branches go with it in the same commit.

Deliberately additive, per the no-destructive-migrations rule: it only inserts
rows for companies that have none for their owner. It never edits an existing
profile, so an owner who already holds a row at some other role -- possible if
ownership was transferred by a route other than registration -- is left exactly
as they are and reported, because demoting or promoting somebody is a decision,
not a data fix.
"""

import logging

from django.db import migrations
from django.db.models import F

logger = logging.getLogger(__name__)


def forwards(apps, schema_editor):
    Company = apps.get_model('company', 'Company')
    CompanyUserProfile = apps.get_model('users', 'CompanyUserProfile')

    existing = set(CompanyUserProfile.objects.values_list('company_id', 'user_id'))
    to_create = [
        CompanyUserProfile(
            company_id=company.id,
            user_id=company.owner_id,
            role='Owner',
            is_active=True,
        )
        for company in Company.objects.exclude(owner__isnull=True).only('id', 'owner_id')
        if (company.id, company.owner_id) not in existing
    ]
    if to_create:
        CompanyUserProfile.objects.bulk_create(to_create, batch_size=500)
        logger.info('owner_profile.backfill created=%d', len(to_create))

    mismatched = (
        CompanyUserProfile.objects
        .filter(user_id=F('company__owner_id'))
        .exclude(role='Owner')
        .values_list('company_id', 'user_id', 'role')
    )
    for company_id, user_id, role in mismatched:
        logger.warning(
            'owner_profile.role_mismatch company=%s user=%s role=%s -- left unchanged',
            company_id, user_id, role,
        )


def backwards(apps, schema_editor):
    """Deliberately a no-op.

    The rows this creates are indistinguishable from the ones
    `register_company_in_transaction` writes for every new company, so there is
    no safe way to tell them apart afterwards. Deleting an owner's membership
    would strip their notification preferences and take them off the roster --
    precisely the breakage this migration exists to fix. Reversing the schema
    does not require reversing the data.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0007_merge_profile_and_theme_fields'),
        ('company', '0003_company_allow_public_projects'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
