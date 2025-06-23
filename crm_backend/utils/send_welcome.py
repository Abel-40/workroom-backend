from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.conf import settings
from datetime import datetime

def send_welcome_email(email_recipient, username):
    try:
        subject = "Welcome to ITDB - Your Employee Management System"
        from_email = settings.EMAIL_HOST_USER  
        to = [email_recipient]

        
        html_content = render_to_string('../templates/welcome_email.html', {
            'username': username,
            'current_year': datetime.now().year,
        })

        
        email = EmailMessage(subject, html_content, from_email, to)
        email.content_subtype = "html"
        email.send()
        return True

    except Exception as e:
        print(f"Error sending email: {e}")
        return False