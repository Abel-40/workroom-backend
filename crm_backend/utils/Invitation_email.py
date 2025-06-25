from django.template.loader import render_to_string
from django.core.mail import EmailMessage
from django.conf import settings

def send_invitation_email(email_recipient, inviter_name, company_name, invitation_link):
    subject = f'You are invited to join {company_name} on Workroom'

    # Render the template with all required context variables
    html_content = render_to_string('emails/invitation_email.html', {
        'inviter_name': inviter_name,
        'company_name': company_name,
        'invitation_link': invitation_link,
    })


    email = EmailMessage(
        subject=subject,
        body=html_content,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email_recipient],
    )
    email.content_subtype = 'html'  
    email.send(fail_silently=False)
