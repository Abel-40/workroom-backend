from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string


def send_notification_email(email_recipient, title, message):
    html_content = render_to_string('emails/notification_email.html', {
        'title': title,
        'message': message,
    })
    email = EmailMessage(
        subject=title,
        body=html_content,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email_recipient],
    )
    email.content_subtype = 'html'
    email.send(fail_silently=False)
