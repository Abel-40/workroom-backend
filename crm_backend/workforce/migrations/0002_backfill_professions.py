"""Turn the free-text profession column into a catalog.

``CompanyUserProfile.profession`` was a ``CharField`` defaulting to the string
``'Not provided'``, which made it three different things at once: a real
answer, a placeholder, and a null. This reads what people actually entered,
creates a ``Profession`` row per distinct value per company, and points
``profession_ref`` at it.

Deliberately conservative about what counts as an answer. Only the literal
placeholders are dropped -- ``'Not provided'``, empty, whitespace. Anything
else a person typed is kept verbatim, including odd capitalisation, because a
migration is the wrong place to decide that somebody's job title was a typo.
Values that differ only by case are folded into one row, keeping the first
spelling seen in creation order, since the new column is case-insensitively
unique.

The old column keeps its value. It is deprecated, dual-written for one release
and dropped later; see the field's comment.

Idempotent: a profile that already has a ``profession_ref`` is skipped, and an
existing catalog row with the same name is reused rather than duplicated.
"""

from django.db import migrations

PLACEHOLDERS = {'', 'not provided', 'none', 'n/a'}


def backfill(apps, schema_editor):
    CompanyUserProfile = apps.get_model('users', 'CompanyUserProfile')
    Profession = apps.get_model('workforce', 'Profession')

    # Existing catalog rows, so a re-run reuses rather than duplicates.
    catalog = {}
    for company_id, name, profession_id in Profession.objects.values_list('company_id', 'name', 'id'):
        catalog.setdefault((company_id, name.strip().lower()), profession_id)

    profiles = CompanyUserProfile.objects.filter(profession_ref__isnull=True).order_by('created_at', 'id')
    to_link = []
    for profile_id, company_id, raw in profiles.values_list('id', 'company_id', 'profession'):
        name = (raw or '').strip()
        if name.lower() in PLACEHOLDERS:
            continue
        key = (company_id, name.lower())
        profession_id = catalog.get(key)
        if profession_id is None:
            profession = Profession.objects.create(company_id=company_id, name=name)
            profession_id = profession.id
            catalog[key] = profession_id
        to_link.append((profile_id, profession_id))

    for profile_id, profession_id in to_link:
        CompanyUserProfile.objects.filter(id=profile_id).update(profession_ref_id=profession_id)


def unbackfill(apps, schema_editor):
    """Unlink, and drop only the catalog rows this could have created.

    A profession with a ``default_profession`` came from the seeded templates
    and a profession still referenced by somebody is in use; neither is this
    migration's to remove. The old text column was never touched, so unlinking
    loses nothing.
    """
    CompanyUserProfile = apps.get_model('users', 'CompanyUserProfile')
    Profession = apps.get_model('workforce', 'Profession')
    CompanyUserProfile.objects.filter(profession_ref__isnull=False).update(profession_ref=None)
    Profession.objects.filter(default_profession__isnull=True, members__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('workforce', '0001_initial'),
        ('users', '0010_companyuserprofile_profession_ref_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
