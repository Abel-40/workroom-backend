"""Composes the RBAC catalog (permissions.catalog) with the existing,
server-derived role resolution in company.services -- this module never
trusts a client-supplied role or company id, it only asks "what role does
this authenticated user actually hold in this company," the same way every
other authorization check in this codebase already does.
"""

from company.services import get_company_role

from .catalog import has_permission


async def user_has_permission(user, company, code: str) -> bool:
    role = await get_company_role(user, company)
    return has_permission(role, code)
