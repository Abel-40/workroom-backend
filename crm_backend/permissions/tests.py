"""RBAC catalog loading/enforcement and the seed_permissions DB mirror.

has_permission() itself never touches the database (see permissions.catalog's
module docstring) -- these tests cover the YAML parsing/validation and the
seed command's idempotency/pruning separately from that hot path.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase
from users.models import CompanyUserProfile

from permissions import catalog
from permissions.models import Permission, RolePermission


class CatalogTests(TestCase):
    def test_role_keys_match_companyuserprofile_role_values(self):
        loaded = catalog.load_catalog(force=True)
        self.assertEqual(set(loaded), set(CompanyUserProfile.Role.values))

    def test_a_malformed_catalog_fails_fast(self):
        bad_yaml = 'roles:\n  Owner:\n    permissions: [company:delete]\n'
        with tempfile.TemporaryDirectory() as tmp_dir:
            bad_path = Path(tmp_dir) / 'bad.yaml'
            bad_path.write_text(bad_yaml, encoding='utf-8')
            with patch.object(catalog, 'CATALOG_PATH', bad_path):
                with self.assertRaises(ImproperlyConfigured):
                    catalog._parse_catalog()
        # Restore the real, valid catalog for every later test in the suite.
        catalog.load_catalog(force=True)

    def test_owner_has_billing_and_cm_management_permissions_that_cm_does_not(self):
        for code in ('subscription:manage', 'members:invite_cm', 'members:manage_cm_role',
                     'company:delete', 'company:transfer_ownership'):
            self.assertTrue(catalog.has_permission('Owner', code), code)
            self.assertFalse(catalog.has_permission('CM', code), code)

    def test_company_manager_has_owner_equivalent_operational_permissions(self):
        for code in ('projects:manage_any', 'tasks:manage_any', 'departments:manage',
                     'teams:manage', 'members:invite', 'members:manage_role', 'documents:delete_any'):
            self.assertTrue(catalog.has_permission('Owner', code), code)
            self.assertTrue(catalog.has_permission('CM', code), code)

    def test_department_leader_lacks_company_wide_management_permissions(self):
        for code in ('projects:manage_any', 'members:manage_role', 'subscription:manage'):
            self.assertFalse(catalog.has_permission('DL', code), code)

    def test_unknown_role_has_no_permissions(self):
        self.assertEqual(catalog.role_permissions('NotARole'), frozenset())
        self.assertFalse(catalog.has_permission(None, 'projects:create'))


class SeedPermissionsCommandTests(TestCase):
    def test_seed_creates_a_row_per_catalog_entry(self):
        call_command('seed_permissions')
        expected_codes = {code for codes in catalog.load_catalog().values() for code in codes}
        expected_grants = sum(len(codes) for codes in catalog.load_catalog().values())
        self.assertEqual(set(Permission.objects.values_list('code', flat=True)), expected_codes)
        self.assertEqual(RolePermission.objects.count(), expected_grants)

    def test_seed_is_idempotent(self):
        call_command('seed_permissions')
        first_permission_count = Permission.objects.count()
        first_grant_count = RolePermission.objects.count()

        call_command('seed_permissions')

        self.assertEqual(Permission.objects.count(), first_permission_count)
        self.assertEqual(RolePermission.objects.count(), first_grant_count)

    def test_seed_prunes_entries_removed_from_the_catalog(self):
        call_command('seed_permissions')
        stale = Permission.objects.create(code='stale:code')
        RolePermission.objects.create(role=CompanyUserProfile.Role.Owner, permission=stale)

        call_command('seed_permissions')

        self.assertFalse(Permission.objects.filter(code='stale:code').exists())
        self.assertFalse(RolePermission.objects.filter(permission__code='stale:code').exists())
