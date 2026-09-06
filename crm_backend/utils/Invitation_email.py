from utils.email import send_branded_email


def send_invitation_email(email_recipient, inviter_name, company_name, invitation_link):
    subject = f'You are invited to join {company_name} on Workroom'
    send_branded_email(email_recipient, subject, 'emails/invitation_email.html', {
        'inviter_name': inviter_name,
        'company_name': company_name,
        'invitation_link': invitation_link,
    })
