from django.db import migrations


class Migration(migrations.Migration):
    """Merges the two 0006 heads that developed in parallel: User.theme /
    welcome_email_sent on one side, CompanyUserProfile.birthday / skype on
    the other.

    The RunSQL repairs a database that ran both branches in sequence. A
    migration on the theme side dropped birthday/skype as presumed schema
    drift -- they are in fact real fields added by the profile side, whose
    AddField is already recorded as applied and so will never re-create
    them. On a fresh database that AddField creates the columns normally and
    this is a no-op.
    """

    dependencies = [
        ('users', '0006_user_theme_user_welcome_email_sent'),
        ('users', '0006_companyuserprofile_birthday_companyuserprofile_skype'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                'ALTER TABLE users_companyuserprofile ADD COLUMN IF NOT EXISTS birthday date NULL;',
                "ALTER TABLE users_companyuserprofile ADD COLUMN IF NOT EXISTS skype varchar(100) NOT NULL DEFAULT '';",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
