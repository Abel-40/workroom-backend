"""Shared branded-email sending. The one place that knows how to embed the
Workroom logo and render/send an HTML email -- utils/Invitation_email.py,
utils/notification_email.py, and utils/welcome_email.py all funnel through
send_branded_email() instead of duplicating the MIME/CID plumbing.
"""

from email.message import MIMEPart
from pathlib import Path

from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string

# CID (Content-ID) embedding, not a data: URI or a static URL: it's the only
# logo-in-email approach that reliably renders across real mail clients
# (including Outlook) regardless of whether this environment's STATIC_URL is
# publicly reachable.
LOGO_PATH = Path(__file__).resolve().parent.parent / 'templates' / 'emails' / 'assets' / 'logo.png'
LOGO_CID = 'workroom-logo'


def send_branded_email(to_email: str, subject: str, template_name: str, context: dict) -> None:
    ctx = {
        **context,
        'logo_cid': LOGO_CID,
        'frontend_url': settings.FRONTEND_URL,
    }
    html_content = render_to_string(template_name, ctx)

    email = EmailMessage(
        subject=subject,
        body=html_content,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to_email],
    )
    email.content_subtype = 'html'

    # email.message.MIMEPart (not the legacy email.mime.image.MIMEImage) --
    # Django 6.0 removed the undocumented `mixed_subtype` attribute this used
    # to rely on, and only takes MIMEPart/plain-tuple attachments cleanly now
    # (a MIMEBase attachment like MIMEImage still works but is deprecated).
    logo = MIMEPart()
    with open(LOGO_PATH, 'rb') as f:
        logo.set_content(
            f.read(), maintype='image', subtype='png',
            disposition='inline', filename='logo.png', cid=f'<{LOGO_CID}>',
        )
    email.attach(logo)

    email.send(fail_silently=False)
