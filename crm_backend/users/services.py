"""Invite-email retry sweep, shared by the Celery Beat periodic task
(users/tasks.py) and the send_pending_invites management command so there is
one implementation instead of two; and CompanyUserProfile role management.
"""

import logging
from zoneinfo import available_timezones

from analytics.services import get_member_workload
from asgiref.sync import sync_to_async
from company.services import get_company_role_sync, get_managed_company_sync, get_member_company, is_company_member_sync
from departments_and_teams.models import Department, Team
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from notifications_and_activity.services import log_member_removed
from permissions.catalog import has_permission
from projects_and_tasks.models import Project, Task
from utils.Invitation_email import send_invitation_email
from utils.tokens import generate_token, hash_token

from .models import CompanyUserProfile, PendingInvite, User

logger = logging.getLogger(__name__)


def purge_expired_invites() -> int:
    """Permanently remove expired invitation records.

    An expired link cannot be accepted or resent: deleting its row means the
    inviter can create a clean new invitation for the same email immediately.
    This is called by the periodic email sweep and again by the send/accept
    paths so correctness does not depend on Celery timing.
    """
    deleted, _ = PendingInvite.objects.filter(expires_at__lte=timezone.now()).delete()
    return deleted


def retry_pending_invite_emails() -> dict:
    """Retry invitation emails for invites where email_sent is still False.

    Only a hash of each invite's token is ever stored (utils/tokens.py), so
    the original email/link can't be reconstructed for a resend -- this
    issues a fresh token instead, which also invalidates whatever token may
    have partially leaked (e.g. into mail server logs) from the failed try.
    """
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://your-frontend.com').rstrip('/')
    expired = purge_expired_invites()
    invites = PendingInvite.objects.filter(
        status=PendingInvite.Status.Pending, email_sent=False,
    ).select_related('company')

    sent = failed = 0
    for invite in invites:
        raw_token = generate_token()
        invite.token_hash = hash_token(raw_token)
        try:
            send_invitation_email(
                invite.email, f'The {invite.company.name} team', invite.company.name,
                f'{frontend_url}/invite/accept?token={raw_token}',
            )
        except Exception:
            invite.save(update_fields=['token_hash'])
            failed += 1
            logger.warning('invite_email.retry_failed', extra={'invite_id': str(invite.id)})
            continue

        invite.email_sent = True
        invite.save(update_fields=['token_hash', 'email_sent'])
        sent += 1

    result = {'sent': sent, 'expired': expired, 'failed': failed}
    logger.info('invite_email.retry_sweep_completed', extra=result)
    return result


async def get_member_profile_picture(requester, target_user_id):
    """Return a member profile only when both users belong to the same company.

    ImageField paths are deliberately never made public; the API router streams
    the file after this membership check.
    """
    company = await get_member_company(requester)
    if company is None:
        return None, 'forbidden'
    profile = await CompanyUserProfile.objects.filter(
        user_id=target_user_id, company=company,
    ).afirst()
    if profile is None or not profile.profile_picture:
        return None, 'not_found'
    return profile, None


def update_member_role(requester, target_user_id, new_role: str):
    """Change a company member's role. Returns (profile, error) where error
    is one of 'forbidden', 'cannot_change_self', 'invalid_target', or None.

    Runs entirely synchronously and inside one transaction -- like
    api.api.accept_invite_in_transaction, this is a multi-write flow
    (the role change, plus clearing Department/Team leadership if someone
    is demoted away from Department Leader) that Django can only make atomic
    on a sync connection.
    """
    with transaction.atomic():
        company = get_managed_company_sync(requester)
        if company is None:
            return None, 'forbidden'
        requester_role = get_company_role_sync(requester, company)
        if not has_permission(requester_role, 'members:manage_role'):
            return None, 'forbidden'
        if target_user_id == requester.id:
            return None, 'cannot_change_self'
        if company.owner_id == target_user_id:
            return None, 'invalid_target'

        profile = CompanyUserProfile.objects.select_related('user').filter(
            user_id=target_user_id, company=company,
        ).first()
        if profile is None:
            return None, 'invalid_target'

        involves_company_manager = CompanyUserProfile.Role.COMPANY_MANAGER in (new_role, profile.role)
        if involves_company_manager and not has_permission(requester_role, 'members:manage_cm_role'):
            return None, 'forbidden'

        if profile.role == new_role:
            return profile, None

        old_role = profile.role
        profile.role = new_role
        profile.save(update_fields=['role'])

        if old_role == CompanyUserProfile.Role.DEPARTMENT_LEADER and new_role != CompanyUserProfile.Role.DEPARTMENT_LEADER:
            Department.objects.filter(company=company, leader_id=target_user_id).update(leader=None)
            Team.objects.filter(company=company, leader_id=target_user_id).update(leader=None)

        return profile, None


def _demote_leader_if_unneeded(company, user_id):
    """After a leadership change, revert the previous leader's role back to
    Department Member if they no longer lead any department or team in this
    company. Mirrors update_member_role's existing demotion-clears-leadership
    logic above, just triggered from the leadership side instead of the role
    side."""
    still_leads = (
        Department.objects.filter(company=company, leader_id=user_id).exists()
        or Team.objects.filter(company=company, leader_id=user_id).exists()
    )
    if still_leads:
        return
    CompanyUserProfile.objects.filter(
        company=company, user_id=user_id, role=CompanyUserProfile.Role.DEPARTMENT_LEADER,
    ).update(role=CompanyUserProfile.Role.DEPARTMENT_MEMBER)


def set_department_leader(requester, department, new_leader_user_id):
    """Assign a company member as a department's leader. Returns
    (department, error) where error is 'forbidden', 'invalid_leader', or None.

    Department.leader alone grants no authority --
    projects_and_tasks.services.user_can_manage_project's Department Leader
    check reads the leader's own CompanyUserProfile.role and .department, not
    Department.leader. So a plain-Member new leader is promoted to DL and
    moved into this department here; an existing Owner/Company Manager keeps
    their own (already broader) role untouched.
    """
    with transaction.atomic():
        company = get_managed_company_sync(requester)
        if company is None or department.company_id != company.id:
            return None, 'forbidden'
        requester_role = get_company_role_sync(requester, company)
        if not has_permission(requester_role, 'departments:manage'):
            return None, 'forbidden'

        new_leader = User.objects.filter(id=new_leader_user_id).first()
        if new_leader is None or not is_company_member_sync(new_leader, company):
            return None, 'invalid_leader'

        previous_leader_id = department.leader_id
        department.leader = new_leader
        department.save(update_fields=['leader'])

        new_leader_profile = CompanyUserProfile.objects.filter(user=new_leader, company=company).first()
        if new_leader_profile is not None and new_leader_profile.role == CompanyUserProfile.Role.DEPARTMENT_MEMBER:
            new_leader_profile.role = CompanyUserProfile.Role.DEPARTMENT_LEADER
            new_leader_profile.department = department
            new_leader_profile.save(update_fields=['role', 'department'])

        if previous_leader_id and previous_leader_id != new_leader.id:
            _demote_leader_if_unneeded(company, previous_leader_id)

        return department, None


def revoke_department_leader(requester, department):
    """Clear a department's leader. Returns (department, error)."""
    with transaction.atomic():
        company = get_managed_company_sync(requester)
        if company is None or department.company_id != company.id:
            return None, 'forbidden'
        requester_role = get_company_role_sync(requester, company)
        if not has_permission(requester_role, 'departments:manage'):
            return None, 'forbidden'

        previous_leader_id = department.leader_id
        department.leader = None
        department.save(update_fields=['leader'])
        if previous_leader_id:
            _demote_leader_if_unneeded(company, previous_leader_id)
        return department, None


def set_team_leader(requester, team, new_leader_user_id):
    """Assign a company member as a team's leader. Unlike a department
    leader, this is a plain label: no authorization in this codebase keys off
    Team.leader (confirmed -- nothing in projects_and_tasks.services reads
    it), so there is no role to sync. Returns (team, error)."""
    with transaction.atomic():
        company = get_managed_company_sync(requester)
        if company is None or team.company_id != company.id:
            return None, 'forbidden'
        requester_role = get_company_role_sync(requester, company)
        if not has_permission(requester_role, 'teams:manage'):
            return None, 'forbidden'

        new_leader = User.objects.filter(id=new_leader_user_id).first()
        if new_leader is None or not is_company_member_sync(new_leader, company):
            return None, 'invalid_leader'

        team.leader = new_leader
        team.save(update_fields=['leader'])
        return team, None


def revoke_team_leader(requester, team):
    """Clear a team's leader. Returns (team, error)."""
    with transaction.atomic():
        company = get_managed_company_sync(requester)
        if company is None or team.company_id != company.id:
            return None, 'forbidden'
        requester_role = get_company_role_sync(requester, company)
        if not has_permission(requester_role, 'teams:manage'):
            return None, 'forbidden'

        team.leader = None
        team.save(update_fields=['leader'])
        return team, None


# --------------------------------------------------------------------------
# Member lifecycle: activate/deactivate, department change, removal
# --------------------------------------------------------------------------

def _get_target_profile(company, target_user_id):
    """Fetches a member's profile for an admin action regardless of its
    current is_active value -- unlike company.services' tenant-resolution
    helpers, an admin managing a member must be able to find (and
    reactivate) an already-deactivated profile."""
    return CompanyUserProfile.objects.select_related('user').filter(
        user_id=target_user_id, company=company,
    ).first()


def set_member_active_status(requester, target_user_id, is_active: bool):
    """Activate/deactivate a member's access to this company. Returns
    (profile, error) where error is 'forbidden', 'cannot_change_self',
    'invalid_target', or None."""
    with transaction.atomic():
        company = get_managed_company_sync(requester)
        if company is None:
            return None, 'forbidden'
        requester_role = get_company_role_sync(requester, company)
        if not has_permission(requester_role, 'members:manage_role'):
            return None, 'forbidden'
        if target_user_id == requester.id:
            return None, 'cannot_change_self'
        if company.owner_id == target_user_id:
            return None, 'invalid_target'

        profile = _get_target_profile(company, target_user_id)
        if profile is None:
            return None, 'invalid_target'

        if profile.is_active != is_active:
            profile.is_active = is_active
            profile.save(update_fields=['is_active'])
        return profile, None


def update_member_department(requester, target_user_id, department_id):
    """Move a member to a different department (or clear it with
    department_id=None). Does not touch leadership or role -- see
    set_department_leader for that, which is the only path that syncs role.
    Returns (profile, error) where error is 'forbidden', 'invalid_target',
    'invalid_department', or None."""
    with transaction.atomic():
        company = get_managed_company_sync(requester)
        if company is None:
            return None, 'forbidden'
        requester_role = get_company_role_sync(requester, company)
        if not has_permission(requester_role, 'members:manage_role'):
            return None, 'forbidden'
        if company.owner_id == target_user_id:
            return None, 'invalid_target'

        profile = _get_target_profile(company, target_user_id)
        if profile is None:
            return None, 'invalid_target'

        department = None
        if department_id is not None:
            department = Department.objects.filter(id=department_id, company=company).first()
            if department is None:
                return None, 'invalid_department'

        profile.department = department
        profile.save(update_fields=['department'])
        return profile, None


async def get_member_detail(requester, target_user_id):
    """Returns (data, error) where data is
    {'user', 'role', 'department_name', 'is_active', 'email_notifications_enabled',
    'workload'} and error is 'forbidden' or 'not_found'."""
    company = await get_member_company(requester)
    if company is None:
        return None, 'forbidden'

    if company.owner_id == target_user_id:
        target = await User.objects.filter(id=target_user_id).afirst()
        if target is None:
            return None, 'not_found'
        workload = await get_member_workload(company, target)
        return {
            'user': target, 'role': CompanyUserProfile.Role.Owner,
            'department_name': None, 'is_active': True,
            'profile_picture_url': None,
            # The owner has no profile row to store a preference on -- always
            # gets critical-only-style behavior, see _should_email's fallback.
            'email_notifications_enabled': True, 'workload': workload,
        }, None

    profile = await CompanyUserProfile.objects.select_related('user', 'department').filter(
        user_id=target_user_id, company=company,
    ).afirst()
    if profile is None:
        return None, 'not_found'
    workload = await get_member_workload(company, profile.user)
    profile_picture_url = (
        f'/company/members/{profile.user_id}/profile-image/' if profile.profile_picture else None
    )
    return {
        'user': profile.user, 'role': profile.role,
        'email_notifications_enabled': profile.email_notifications_enabled,
        'department_name': profile.department.name if profile.department_id else None,
        'profile_picture_url': profile_picture_url,
        'is_active': profile.is_active, 'workload': workload,
    }, None


def get_removal_blockers(company, target_user_id) -> dict:
    """Active work the departing member currently owns/is assigned --
    the caller must resolve these (see remove_member) before removal."""
    projects = list(
        Project.objects.filter(
            company=company, current_owner_id=target_user_id, is_deleted=False,
        ).exclude(status=Project.STATUS.DONE).values('id', 'title'),
    )
    tasks = list(
        Task.objects.filter(
            project__company=company, assigned_to_id=target_user_id, is_deleted=False,
        ).exclude(status=Task.STATUS.DONE).values('id', 'title'),
    )
    return {
        'projects': [{'id': str(p['id']), 'title': p['title']} for p in projects],
        'tasks': [{'id': str(t['id']), 'title': t['title']} for t in tasks],
    }


def remove_member(requester, target_user_id, *, reassign_to_user_id=None):
    """Remove a member from the company. If they currently own active
    projects or are assigned active tasks, a reassignment target is required
    -- this function never silently picks one. Returns (result, error) where
    error is 'forbidden', 'cannot_change_self', 'invalid_target',
    'invalid_reassignee', 'reassignment_required', or None. On
    'reassignment_required', result is the blockers dict (see
    get_removal_blockers); otherwise result is {'reassigned_count': int}.
    """
    with transaction.atomic():
        company = get_managed_company_sync(requester)
        if company is None:
            return None, 'forbidden'
        requester_role = get_company_role_sync(requester, company)
        if not has_permission(requester_role, 'members:remove'):
            return None, 'forbidden'
        if target_user_id == requester.id:
            return None, 'cannot_change_self'
        if company.owner_id == target_user_id:
            return None, 'invalid_target'

        profile = _get_target_profile(company, target_user_id)
        if profile is None:
            return None, 'invalid_target'
        if profile.role == CompanyUserProfile.Role.COMPANY_MANAGER and not has_permission(
            requester_role, 'members:manage_cm_role',
        ):
            # Removing a peer Company Manager is at least as sensitive as
            # promoting/demoting one -- same Owner-only gate as
            # update_member_role's involves_company_manager check.
            return None, 'forbidden'

        blockers = get_removal_blockers(company, target_user_id)
        reassigned_count = 0
        if blockers['projects'] or blockers['tasks']:
            if reassign_to_user_id is None:
                return blockers, 'reassignment_required'
            reassignee = User.objects.filter(id=reassign_to_user_id).first()
            if reassignee is None or not is_company_member_sync(reassignee, company):
                return None, 'invalid_reassignee'
            if blockers['projects']:
                reassigned_count += Project.objects.filter(
                    id__in=[p['id'] for p in blockers['projects']],
                ).update(current_owner=reassignee)
            if blockers['tasks']:
                reassigned_count += Task.objects.filter(
                    id__in=[t['id'] for t in blockers['tasks']],
                ).update(assigned_to=reassignee)

        Department.objects.filter(company=company, leader_id=target_user_id).update(leader=None)
        Team.objects.filter(company=company, leader_id=target_user_id).update(leader=None)
        for team in Team.objects.filter(company=company, members=target_user_id):
            team.members.remove(target_user_id)

        removed_user = profile.user
        profile.delete()
        log_member_removed(company, requester, removed_user, reassigned_count=reassigned_count)
        return {'reassigned_count': reassigned_count}, None


async def update_notification_preference(user, email_notifications_enabled: bool):
    """Self-service: a member updates their own email-notification
    preference. Returns (profile, error) where error is 'forbidden' (no
    company) or 'no_profile' (the company owner has no CompanyUserProfile
    row to store a preference on -- they always get critical-only behavior,
    see notifications_and_activity.services._should_email)."""
    company = await get_member_company(user)
    if company is None:
        return None, 'forbidden'
    profile = await CompanyUserProfile.objects.filter(user=user, company=company).afirst()
    if profile is None:
        return None, 'no_profile'
    if profile.email_notifications_enabled != email_notifications_enabled:
        profile.email_notifications_enabled = email_notifications_enabled
        await profile.asave(update_fields=['email_notifications_enabled'])
    return profile, None


async def update_user_timezone(user, tz_name: str):
    """Self-service: a user updates their own display timezone. Unlike
    update_notification_preference above, this never depends on company
    membership -- timezone lives on User (see users.models.User.timezone),
    not CompanyUserProfile, specifically so it works for every authenticated
    user including a company Owner. Returns (user, error) where error is
    'invalid_timezone' or None."""
    if tz_name not in available_timezones():
        return None, 'invalid_timezone'
    if user.timezone != tz_name:
        user.timezone = tz_name
        await user.asave(update_fields=['timezone'])
    return user, None


# --------------------------------------------------------------------------
# Self-service profile fields (birthday/skype are new; profession/address/
# phone_number/resume already existed on CompanyUserProfile) -- same
# no-company-profile gap as update_notification_preference above (the
# company owner has no CompanyUserProfile row).
# --------------------------------------------------------------------------

PROFILE_UPDATABLE_FIELDS = {'profession', 'address', 'phone_number', 'birthday', 'skype'}

MAX_RESUME_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB -- smaller than the general 10MB document cap.
ALLOWED_RESUME_CONTENT_TYPES = {
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}


async def get_own_profile(user):
    """Self-service: fetch the caller's own CompanyUserProfile fields, e.g.
    to hydrate a profile-edit form. Returns (profile, error) where error is
    'forbidden' (no company) or 'no_profile' (the company owner has no
    profile row), or None."""
    company = await get_member_company(user)
    if company is None:
        return None, 'forbidden'
    profile = await CompanyUserProfile.objects.filter(user=user, company=company).afirst()
    if profile is None:
        return None, 'no_profile'
    return profile, None


async def update_own_profile(user, updates: dict):
    """Self-service: a member updates their own CompanyUserProfile fields.
    Returns (profile, error) where error is 'forbidden' (no company) or
    'no_profile' (the company owner has no profile row), or None."""
    company = await get_member_company(user)
    if company is None:
        return None, 'forbidden'
    profile = await CompanyUserProfile.objects.filter(user=user, company=company).afirst()
    if profile is None:
        return None, 'no_profile'
    update_fields = [field for field in updates if field in PROFILE_UPDATABLE_FIELDS]
    for field in update_fields:
        setattr(profile, field, updates[field])
    if update_fields:
        await profile.asave(update_fields=update_fields)
    return profile, None


async def upload_own_resume(user, uploaded_file):
    """Self-service resume upload. Returns (profile, error) where error is
    'forbidden', 'no_profile', 'too_large', 'invalid_content_type', or
    None."""
    company = await get_member_company(user)
    if company is None:
        return None, 'forbidden'
    profile = await CompanyUserProfile.objects.filter(user=user, company=company).afirst()
    if profile is None:
        return None, 'no_profile'
    if uploaded_file.size > MAX_RESUME_SIZE_BYTES:
        return None, 'too_large'
    content_type = uploaded_file.content_type or ''
    if content_type not in ALLOWED_RESUME_CONTENT_TYPES:
        return None, 'invalid_content_type'
    if profile.resume:
        await sync_to_async(profile.resume.delete, thread_sensitive=True)(save=False)
    profile.resume = uploaded_file
    await profile.asave(update_fields=['resume'])
    return profile, None


async def get_own_resume(user):
    """Self-service resume lookup, used to stream it back to its owner only
    -- mirrors get_member_profile_picture's ownership-scoped read. Returns
    (profile, error) where error is 'forbidden' or 'not_found'."""
    company = await get_member_company(user)
    if company is None:
        return None, 'forbidden'
    profile = await CompanyUserProfile.objects.filter(user=user, company=company).afirst()
    if profile is None or not profile.resume:
        return None, 'not_found'
    return profile, None
