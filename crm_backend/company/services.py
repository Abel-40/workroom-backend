"""Authenticated company-context resolution.

The authoritative company for a mutation is always derived from the
authenticated user's server-side state (ownership or profile role), never
from a client-supplied ``company_id``. Centralized here so every endpoint
that needs "which company may this user manage" resolves it the same way.
"""

from audit.services import AuditAction, arecord_event
from users.models import CompanyUserProfile

from company.models import Company


async def get_owned_company(user) -> Company | None:
    """The company this user owns, if any."""
    return await Company.objects.filter(owner=user).afirst()


MANAGED_COMPANY_ROLES = (
    CompanyUserProfile.Role.COMPANY_MANAGER,
    CompanyUserProfile.Role.DEPARTMENT_LEADER,
)


async def get_managed_company(user) -> Company | None:
    """The company this user may manage: the company they own, or, failing
    that, the company where they hold an active Company Manager or
    department-leader profile."""
    company = await get_owned_company(user)
    if company is not None:
        return company
    profile = await CompanyUserProfile.objects.select_related('company').filter(
        user=user, role__in=MANAGED_COMPANY_ROLES, is_active=True,
    ).afirst()
    return profile.company if profile else None


async def get_member_company(user) -> Company | None:
    """The company this user belongs to at all: the company they own, or the
    company of any active membership profile they hold, regardless of role.
    This is the baseline check for "may this user act within this company's
    data," as opposed to :func:`get_managed_company`, which is admin-only
    actions.

    A deactivated profile (CompanyUserProfile.is_active=False) resolves to
    "no company" here -- the same outcome as having no profile at all -- so a
    deactivated member's JWT still authenticates, but every company-scoped
    endpoint they call 404s, without touching Django's own auth.is_active.
    """
    company = await get_owned_company(user)
    if company is not None:
        return company
    profile = await CompanyUserProfile.objects.select_related('company').filter(
        user=user, is_active=True,
    ).afirst()
    return profile.company if profile else None


async def is_company_member(user, company: Company) -> bool:
    """Whether ``user`` (owner or any active profile role) belongs to
    ``company``.

    Used to validate an arbitrary target user (an assignee, a collaborator,
    a leader, a new project owner) against a company, not just the
    requesting user -- a deactivated member is not a valid target.
    """
    if company.owner_id == user.id:
        return True
    return await CompanyUserProfile.objects.filter(user=user, company=company, is_active=True).aexists()


async def get_company_role(user, company: Company) -> str | None:
    """'Owner' if the user owns the company, else their active profile role,
    else None."""
    if company.owner_id == user.id:
        return CompanyUserProfile.Role.Owner
    profile = await CompanyUserProfile.objects.filter(user=user, company=company, is_active=True).afirst()
    return profile.role if profile else None


async def get_member_department_id(user, company: Company):
    """The department this user belongs to, if any. The owner is never
    department-scoped; returns None for them same as for a member with no
    department assigned."""
    if company.owner_id == user.id:
        return None
    profile = await CompanyUserProfile.objects.filter(user=user, company=company, is_active=True).afirst()
    return profile.department_id if profile else None


# --------------------------------------------------------------------------
# Sync mirrors
# --------------------------------------------------------------------------
# Django transactions are sync-only, so a multi-write flow that must be
# atomic (see users.services.update_member_role, and api.api's existing
# accept_invite_in_transaction) can't await these primitives mid-transaction.
# These mirror the exact same logic with sync ORM calls, for that use only --
# prefer the async versions above everywhere else.

def get_owned_company_sync(user) -> Company | None:
    return Company.objects.filter(owner=user).first()


def get_managed_company_sync(user) -> Company | None:
    company = get_owned_company_sync(user)
    if company is not None:
        return company
    profile = CompanyUserProfile.objects.select_related('company').filter(
        user=user, role__in=MANAGED_COMPANY_ROLES, is_active=True,
    ).first()
    return profile.company if profile else None


def get_company_role_sync(user, company: Company) -> str | None:
    if company.owner_id == user.id:
        return CompanyUserProfile.Role.Owner
    profile = CompanyUserProfile.objects.filter(user=user, company=company, is_active=True).first()
    return profile.role if profile else None


def get_member_department_id_sync(user, company: Company):
    if company.owner_id == user.id:
        return None
    profile = CompanyUserProfile.objects.filter(user=user, company=company, is_active=True).first()
    return profile.department_id if profile else None


def is_company_member_sync(user, company: Company) -> bool:
    if company.owner_id == user.id:
        return True
    return CompanyUserProfile.objects.filter(user=user, company=company, is_active=True).exists()


# --------------------------------------------------------------------------
# Company settings
# --------------------------------------------------------------------------
# Settings that change what the company as a whole permits, as opposed to the
# per-resource permissions in projects_and_tasks.access. There is one so far.

COMPANY_SETTINGS_FIELDS = ('allow_public_projects',)


async def get_company_settings(user):
    """Returns (company, error) where error is 'not_found' or None.

    Readable by any active member, not just the Owner: the project form has to
    know whether `public` is on the menu before it offers it, and a member who
    could not read the setting would meet the refusal only after filling the
    form in.
    """
    company = await get_member_company(user)
    if company is None:
        return None, 'not_found'
    return company, None


async def update_company_settings(user, updates: dict):
    """Returns (company, error) where error is 'forbidden' or None.

    Owner-only, and deliberately narrower than :func:`get_managed_company`,
    which also admits Company Managers and department leaders.
    ``allow_public_projects`` decides whether this company's work can leave the
    tenant boundary at all -- that is a statement about the company's own
    exposure, so it belongs to the one person accountable for it rather than to
    everyone who can administer the company.
    """
    company = await get_owned_company(user)
    if company is None:
        return None, 'forbidden'

    before, after = {}, {}
    for field in COMPANY_SETTINGS_FIELDS:
        if updates.get(field) is None:
            # Absent and explicitly null both mean "not being set". The schema
            # types these as optional so a PATCH naming one setting does not
            # reset the others, and `None` is that absence -- writing it would
            # put a null in a non-nullable column.
            continue
        current = getattr(company, field)
        if updates[field] == current:
            continue
        before[field] = current
        after[field] = updates[field]
        setattr(company, field, updates[field])

    if not after:
        # A no-op write is not a change, and recording one would put rows in the
        # audit log that say nothing happened.
        return company, None

    await company.asave(update_fields=list(after))
    await arecord_event(
        company=company, actor=user, action=AuditAction.COMPANY_SETTINGS_CHANGED, target=company,
        before=before, after=after,
    )
    return company, None
