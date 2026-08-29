"""DESTRUCTIVE: wipes every company/user and everything that hangs off them
(departments, teams, projects, tasks, attachments, events, pages, AI
generations, notifications, invitations, subscriptions...) so a dev/test
database can be exercised from a clean slate.

Deliberately narrow about what it deletes: only `Company` (relied on to
cascade through the entire tenant tree via each model's own on_delete=CASCADE
-- see departments_and_teams/projects_and_tasks/event_management/pages/
notifications_and_activity/users' Company FKs) and `User` rows. Everything
else -- the RBAC permission/role-grant mirror (permissions app), Plan tiers,
Sector list, and the Default*Type onboarding templates (DefaultDepartment/
DefaultTaskType/DefaultEventType, all keyed by sector, never by company) --
is global reference data seeded once by the existing seed_* commands and is
never touched here.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company

User = get_user_model()


class Command(BaseCommand):
    help = (
        "DESTRUCTIVE: deletes all companies and users (cascading through every "
        "tenant-scoped table). Keeps permissions/role grants, plans, sectors, "
        "and default department/task-type/event-type templates. Dev/test only."
    )

    def add_arguments(self, parser):
        parser.add_argument('--yes', action='store_true', help='Required to actually run.')
        parser.add_argument(
            '--include-superusers', action='store_true',
            help='Also delete superuser accounts (default: superusers are preserved).',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('Refusing to run with DEBUG=False (production settings).')
        if not options['yes']:
            raise CommandError('This deletes ALL company and user data. Pass --yes to confirm.')

        user_qs = User.objects.all() if options['include_superusers'] else User.objects.filter(is_superuser=False)

        with transaction.atomic():
            user_accounts_to_delete = user_qs.count()
            company_count, company_detail = Company.objects.all().delete()
            user_total, user_detail = user_qs.delete()

        self.stdout.write(self.style.SUCCESS(
            f'Deleted {company_count} row(s) cascading from Company ({company_detail}); '
            f'deleted {user_accounts_to_delete} user account(s), {user_total} row(s) total '
            f'cascading from them ({user_detail}). '
            f'Preserved: permissions/role grants, plans, sectors, default department/task-type/event-type templates.'
        ))
