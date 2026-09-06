from utils.email import send_branded_email

# Keys must match notifications_and_activity.models.Notification.Type values.
# Not imported directly -- utils/ stays app-agnostic, and the caller already
# only ever passes a real Notification.Type value here.
TEMPLATE_MAP = {
    'task_assigned': 'emails/notifications/task_assigned_email.html',
    'task_completed': 'emails/notifications/task_completed_email.html',
    'invitation_accepted': 'emails/notifications/invitation_accepted_email.html',
    'ai_generation_completed': 'emails/notifications/ai_generation_completed_email.html',
    'ai_generation_failed': 'emails/notifications/ai_generation_failed_email.html',
}


def send_notification_email(email_recipient, title, message, notification_type=''):
    template = TEMPLATE_MAP.get(notification_type, 'emails/notification_email.html')
    send_branded_email(email_recipient, title, template, {
        'title': title,
        'message': message,
    })
