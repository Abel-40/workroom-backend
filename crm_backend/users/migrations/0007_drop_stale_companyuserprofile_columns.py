from django.db import migrations


class Migration(migrations.Migration):
    """`skype` and `birthday` exist on the live `users_companyuserprofile`
    table but were never part of any CompanyUserProfile field or migration
    in this app's history -- pure schema drift from before this app's
    migrations were tracked. `skype` is NOT NULL with no default, so every
    insert Django makes (which never sets it, since it isn't a model field)
    violates that constraint -- see api.register_company's
    NotNullViolation. Dropping both reconciles the database with the
    model."""

    dependencies = [
        ('users', '0006_user_theme_user_welcome_email_sent'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                'ALTER TABLE users_companyuserprofile DROP COLUMN IF EXISTS skype;',
                'ALTER TABLE users_companyuserprofile DROP COLUMN IF EXISTS birthday;',
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
