"""The one place that answers "what may this user do on this project".

Every project-scoped permission decision in the codebase resolves through
:func:`resolve_project_access`. Nothing else compares a role to a string, reads
``project.created_by`` to decide rights, or re-derives a department match. When
the access model changes, it changes here.

The three levels are deliberately ordered and deliberately do not blur:

``VIEW``
    Discovery. You can find it and read it. Nothing else.

``CONTRIBUTE``
    Work on what you have been given: move your own tasks, submit evidence,
    log time, comment, upload.

``MANAGE``
    Shape the work: edit the project, create and assign tasks, move deadlines,
    transfer ownership, change visibility within the limits set elsewhere.

A higher level includes the ones below it. That is the one property the
scattered predicates this replaces did *not* have -- 30 combinations granted
management of a project the same user could not open -- and restoring it is the
point of using an ordered type.

What this function is not: it is not the company-role system. Company and
department structure, member roles and billing sit above it, unchanged, in the
YAML permission catalog and ``users.services``.
"""

from django.db import models
from users.models import CompanyUserProfile

from .models import Project, ProjectMembership


class AccessLevel(models.IntegerChoices):
    """Ordered, so ``max()`` over several grants is the effective access and
    ``>=`` is the check. Spaced by tens to leave room for a level between two
    existing ones without renumbering stored data."""

    VIEW = 10, 'View'
    CONTRIBUTE = 20, 'Contribute'
    MANAGE = 30, 'Manage'


MANAGE_ANY_ROLES = (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER)

MEMBERSHIP_ACCESS = {
    ProjectMembership.Role.VIEWER: AccessLevel.VIEW,
    ProjectMembership.Role.CONTRIBUTOR: AccessLevel.CONTRIBUTE,
    ProjectMembership.Role.MANAGER: AccessLevel.MANAGE,
}


async def company_standing(user, company):
    """``(role, department_id)`` for this user in this company, or
    ``(None, None)`` if they are not an active member.

    Mirrors ``company.services.get_company_role`` and
    ``get_member_department_id`` exactly -- including the company owner
    resolving to ``Owner`` with no department whether or not they hold a
    profile row -- but answers both in one query instead of two, because every
    access decision needs both.
    """
    if company.owner_id == user.id:
        return CompanyUserProfile.Role.Owner, None
    profile = await CompanyUserProfile.objects.filter(
        user=user, company=company, is_active=True,
    ).only('role', 'department_id').afirst()
    if profile is None:
        return None, None
    return profile.role, profile.department_id


async def resolve_project_access(user, project) -> AccessLevel | None:
    """The effective access this user has on this project, or ``None``.

    Effective access is the **maximum** of every grant that applies, so adding
    a source can only ever widen access for the people it names, never narrow
    it for anyone else.
    """
    grants = []

    # Checked before membership, on purpose. A public project is meant to sit
    # outside the tenant boundary -- that is what the visibility means. Who is
    # allowed to *put* a project there is a separate question, answered at the
    # transition (Owner/CM, behind a company flag), not here.
    if project.visibility == Project.VISIBILITY.PUBLIC:
        grants.append(AccessLevel.VIEW)

    role, department_id = await company_standing(user, project.company)
    if role is None:
        # Not an active member. No per-project reference -- created_by,
        # current_owner, an assignment -- grants anything from here on. A
        # reference can outlive the membership behind it: created_by is
        # deliberately left intact when someone is removed from a company, as
        # immutable provenance, and provenance must not double as a grant.
        return max(grants, default=None)

    # -- company standing ---------------------------------------------------
    if role in MANAGE_ANY_ROLES:
        grants.append(AccessLevel.MANAGE)
    elif (
        role == CompanyUserProfile.Role.DEPARTMENT_LEADER
        and project.department_id and department_id == project.department_id
    ):
        grants.append(AccessLevel.MANAGE)

    # -- the accountable owner ----------------------------------------------
    if project.current_owner_id == user.id:
        grants.append(AccessLevel.MANAGE)

    # -- provenance ---------------------------------------------------------
    # created_by is permanent, immutable and grants VIEW only. It records who
    # started the project, which is worth keeping forever; it is not a claim
    # on the project, which is what current_owner and a manager membership are
    # for. Anyone who genuinely needs to keep managing what they created gets
    # a manager membership -- the backfill gives every existing creator one.
    if project.created_by_id == user.id:
        grants.append(AccessLevel.VIEW)

    # -- explicit per-person grants ----------------------------------------
    membership_role = await ProjectMembership.objects.filter(
        project=project, user=user,
    ).values_list('role', flat=True).afirst()
    if membership_role is not None:
        grants.append(MEMBERSHIP_ACCESS[membership_role])

    # -- being given work to do --------------------------------------------
    # An assignment is itself a grant: somebody with MANAGE decided this
    # person should do this task, and they cannot do it without being able to
    # reach the project. Live tasks only -- a deleted task, or one on an
    # archived project, grants nothing.
    if AccessLevel.CONTRIBUTE not in grants and await project.tasks.filter(
        assigned_to=user, is_deleted=False,
    ).aexists():
        grants.append(AccessLevel.CONTRIBUTE)

    # -- visibility ---------------------------------------------------------
    # Discovery only, never capability. This is the rule the whole model turns
    # on: widening a project's visibility lets more people find it and must
    # never let more people change it.
    if project.visibility == Project.VISIBILITY.COMPANY:
        grants.append(AccessLevel.VIEW)
    elif (
        project.visibility == Project.VISIBILITY.DEPARTMENT
        and project.department_id and department_id == project.department_id
    ):
        grants.append(AccessLevel.VIEW)

    return max(grants, default=None)
