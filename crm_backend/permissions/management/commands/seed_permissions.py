"""Seed the Permission/RolePermission DB mirror from the RBAC YAML catalog
(permissions and roles/roles_permission.yaml).

This must run after `migrate` (see docker-compose.yml / DEPLOYMENT.md), but
nothing on the request path depends on it having run: has_permission() reads
the YAML directly (see permissions.catalog), never these tables. This
command exists purely to keep an auditable, queryable mirror in the database
for introspection and a future admin UI -- idempotent get_or_create, same
convention as seed_sectors/seed_plans/seed_default_departments, plus a prune
pass so the mirror never drifts from the YAML if entries are removed.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from permissions.catalog import load_catalog
from permissions.models import Permission, RolePermission


class Command(BaseCommand):
    help = 'Seed the Permission/RolePermission tables from permissions and roles/roles_permission.yaml'

    def handle(self, *args, **options):
        catalog = load_catalog(force=True)
        all_codes = {code for codes in catalog.values() for code in codes}

        with transaction.atomic():
            permissions_by_code = {p.code: p for p in Permission.objects.all()}
            created_permissions = 0
            for code in sorted(all_codes - permissions_by_code.keys()):
                permissions_by_code[code] = Permission.objects.create(code=code)
                created_permissions += 1

            wanted_pairs = {
                (role, permissions_by_code[code].id)
                for role, codes in catalog.items()
                for code in codes
            }
            existing_grants = {(rp.role, rp.permission_id): rp for rp in RolePermission.objects.all()}

            created_grants = 0
            for role, permission_id in wanted_pairs - existing_grants.keys():
                RolePermission.objects.create(role=role, permission_id=permission_id)
                created_grants += 1

            stale_grant_ids = [
                rp.id for pair, rp in existing_grants.items() if pair not in wanted_pairs
            ]
            deleted_grants = 0
            if stale_grant_ids:
                deleted_grants, _ = RolePermission.objects.filter(id__in=stale_grant_ids).delete()

            stale_permission_ids = [
                perm.id for code, perm in permissions_by_code.items() if code not in all_codes
            ]
            deleted_permissions = 0
            if stale_permission_ids:
                deleted_permissions, _ = Permission.objects.filter(id__in=stale_permission_ids).delete()

        self.stdout.write(self.style.SUCCESS(
            f'Permissions: +{created_permissions} created, {deleted_permissions} pruned. '
            f'Role grants: +{created_grants} created, {deleted_grants} pruned.'
        ))
