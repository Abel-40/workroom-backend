from utils.email import send_branded_email


def send_welcome_email(email_recipient, username):
    send_branded_email(email_recipient, 'Welcome to Workroom', 'emails/welcome_email.html', {
        'username': username,
    })
