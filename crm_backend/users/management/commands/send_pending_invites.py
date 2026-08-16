from django.conf import settings
from django.core.management.base import BaseCommand
from utils.Invitation_email import send_invitation_email
from utils.tokens import generate_token, hash_token

from users.models import PendingInvite


class Command(BaseCommand):
    """Retry invitation emails for invites where email_sent is still False.

    Only a hash of each invite's token is ever stored (utils/tokens.py), so
    the original email/link can't be reconstructed for a resend -- this
    issues a fresh token instead, which also invalidates whatever token may
    have partially leaked (e.g. into mail server logs) from the failed try.
    """

    help = "Retry sending invitation emails for pending invites that previously failed to send"

    def handle(self, *args, **options):
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
            except Exception as exc:
                invite.save(update_fields=['token_hash'])
                failed += 1
                self.stdout.write(self.style.WARNING(f"Failed to send to {invite.email}: {exc}"))
                continue

            invite.email_sent = True
            invite.save(update_fields=['token_hash', 'email_sent'])
            sent += 1
            self.stdout.write(self.style.SUCCESS(f"Sent invite email to {invite.email}"))

        self.stdout.write(self.style.SUCCESS(f"Done. sent={sent} expired={expired} failed={failed}"))
