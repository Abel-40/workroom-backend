"""Loads the Workroom RBAC catalog -- the single source of truth for which
permissions each company role holds.

Parsed once, at process start (see permissions.apps.PermissionsConfig.ready),
from ``permissions and roles/roles_permission.yaml`` into an in-memory
``{role_code: frozenset(permission_codes)}`` map. ``has_permission`` never
queries the database: permissions.models.Permission/RolePermission are a DB
mirror of this same file, seeded by ``manage.py seed_permissions`` purely for
auditability and a future admin UI, and are never read on this hot path -- a
skipped or failed seed run degrades only that introspection, never live
authorization.
"""

from pathlib import Path

import yaml
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

CATALOG_PATH = Path(settings.BASE_DIR) / 'permissions and roles' / 'roles_permission.yaml'

_catalog: dict[str, frozenset[str]] | None = None


def load_catalog(force: bool = False) -> dict[str, frozenset[str]]:
    """Returns the cached {role: frozenset(permission_codes)} map, parsing
    the YAML file on first call (or when ``force`` is True)."""
    global _catalog
    if _catalog is None or force:
        _catalog = _parse_catalog()
    return _catalog


def _parse_catalog() -> dict[str, frozenset[str]]:
    from users.models import CompanyUserProfile

    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding='utf-8')) or {}
    roles = raw.get('roles') or {}
    catalog = {role: frozenset(data.get('permissions') or []) for role, data in roles.items()}

    expected = set(CompanyUserProfile.Role.values)
    found = set(catalog)
    if found != expected:
        raise ImproperlyConfigured(
            f'{CATALOG_PATH} defines roles {sorted(found)}, which does not match '
            f'CompanyUserProfile.Role values {sorted(expected)}.'
        )
    return catalog


def role_permissions(role: str | None) -> frozenset[str]:
    if not role:
        return frozenset()
    return load_catalog().get(role, frozenset())


def has_permission(role: str | None, code: str) -> bool:
    return code in role_permissions(role)
