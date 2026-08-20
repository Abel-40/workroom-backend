"""Invite-email retry sweep, shared by the Celery Beat periodic task
(users/tasks.py) and the send_pending_invites management command so there is
one implementation instead of two; and CompanyUserProfile role management.
"""

import logging

from company.services import get_company_role_sync, get_managed_company_sync
from departments_and_teams.models import Department, Team
from django.conf import settings
from django.db import transaction
from permissions.catalog import has_permission
from utils.Invitation_email import send_invitation_email
from utils.tokens import generate_token, hash_token

from .models import CompanyUserProfile, PendingInvite

logger = logging.getLogger(__name__)


def retry_pending_invite_emails() -> dict:
    """Retry invitation emails for invites where email_sent is still False.

    Only a hash of each invite's token is ever stored (utils/tokens.py), so
    the original email/link can't be reconstructed for a resend -- this
    issues a fresh token instead, which also invalidates whatever token may
    have partially leaked (e.g. into mail server logs) from the failed try.
    """
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://your-frontend.com').rstrip('/')
    invites = PendingInvite.objects.filter(
        status=PendingInvite.Status.Pending, email_sent=False,
    ).select_related('company')

    sent = expired = failed = 0
    for invite in invites:
        if invite.is_expired():
            invite.status = PendingInvite.Status.Expired
            invite.save(update_fields=['status'])
            expired += 1
            continue

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
