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
    'task_submitted_for_approval': 'emails/notifications/task_submitted_for_approval_email.html',
    'task_approved': 'emails/notifications/task_approved_email.html',
    'task_rejected': 'emails/notifications/task_rejected_email.html',
    'deadline_extended': 'emails/notifications/deadline_extended_email.html',
    'project_auto_completed': 'emails/notifications/project_auto_completed_email.html',
    'visibility_requested': 'emails/notifications/visibility_requested_email.html',
    'visibility_approved': 'emails/notifications/visibility_approved_email.html',
    'visibility_denied': 'emails/notifications/visibility_denied_email.html',
    'project_completed': 'emails/notifications/project_completed_email.html',
    'project_reopened': 'emails/notifications/project_reopened_email.html',
    'project_ownership_transferred': 'emails/notifications/project_ownership_transferred_email.html',
    'folder_shared': 'emails/notifications/folder_shared_email.html',
    'todos_generated': 'emails/notifications/todos_generated_email.html',
    'todos_generation_failed': 'emails/notifications/todos_generation_failed_email.html',
}


def send_notification_email(email_recipient, title, message, notification_type=''):
    template = TEMPLATE_MAP.get(notification_type, 'emails/notification_email.html')
    send_branded_email(email_recipient, title, template, {
        'title': title,
        'message': message,
    })
