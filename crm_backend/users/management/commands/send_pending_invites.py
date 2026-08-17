from django.core.management.base import BaseCommand

from users.services import retry_pending_invite_emails


class Command(BaseCommand):
    help = "Retry sending invitation emails for pending invites that previously failed to send"

    def handle(self, *args, **options):
        result = retry_pending_invite_emails()
        self.stdout.write(self.style.SUCCESS(
            f"Done. sent={result['sent']} expired={result['expired']} failed={result['failed']}"
        ))
