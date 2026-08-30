from django.conf import settings
from django.db import models
from utils.models import UUIDModel


class CompanyActivity(UUIDModel):
    """A curated company-wide event feed -- deliberately not one entry per
    minor field edit (see notifications_and_activity.services.log_activity
    and its call sites). Distinct from Notification, which is user-specific
    and permission/role-aware; this is company-wide and any member may see
    it (same posture as analytics:view)."""

    class ActivityType(models.TextChoices):
        PROJECT_CREATED = 'project_created', 'Project Created'
        PROJECT_COMPLETED = 'project_completed', 'Project Completed'
        PROJECT_OWNERSHIP_TRANSFERRED = 'project_ownership_transferred', 'Ownership Transferred'
        MEMBER_INVITED = 'member_invited', 'Member Invited'
        MEMBER_JOINED = 'member_joined', 'Member Joined'
        MEMBER_REMOVED = 'member_removed', 'Member Removed'
        DEPARTMENT_CREATED = 'department_created', 'Department Created'
        TEAM_CREATED = 'team_created', 'Team Created'

    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='activities')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    type = models.CharField(max_length=40, choices=ActivityType.choices)
    # Precomputed at creation time so a list request never needs a join to
    # render -- and stays readable even if the related project/user is later
    # renamed or removed.
    summary = models.CharField(max_length=255)
    # Same loose (no hard FK) reference convention as Notification below.
    related_object_type = models.CharField(max_length=50, blank=True, default='')
    related_object_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'company activities'

    def __str__(self):
        return f'{self.type} -> {self.company_id}'


class Notification(UUIDModel):
    class Type(models.TextChoices):
        TASK_ASSIGNED = 'task_assigned', 'Task Assigned'
        TASK_COMPLETED = 'task_completed', 'Task Completed'
        INVITATION_ACCEPTED = 'invitation_accepted', 'Invitation Accepted'
        AI_GENERATION_COMPLETED = 'ai_generation_completed', 'AI Generation Completed'
        AI_GENERATION_FAILED = 'ai_generation_failed', 'AI Generation Failed'
        TASK_SUBMITTED_FOR_APPROVAL = 'task_submitted_for_approval', 'Task Submitted For Approval'
        TASK_APPROVED = 'task_approved', 'Task Approved'
        TASK_REJECTED = 'task_rejected', 'Task Rejected'
        DEADLINE_EXTENDED = 'deadline_extended', 'Deadline Extended'
        PROJECT_AUTO_COMPLETED = 'project_auto_completed', 'Project Auto-Completed'

    class Category(models.TextChoices):
        CRITICAL = 'critical', 'Critical'
        OPTIONAL = 'optional', 'Optional'

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    type = models.CharField(max_length=30, choices=Type.choices)
    # Critical notifications always email regardless of the recipient's
    # preference; optional ones respect CompanyUserProfile.email_notifications_enabled
    # -- see notifications_and_activity.services.TYPE_CATEGORY.
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.OPTIONAL)
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, default='')
    # Loose reference (no hard FK) to whatever this notification is about --
    # a project, task, company, or AI generation. Simpler than a
    # GenericForeignKey for what's currently a read-only, display-only link.
    related_object_type = models.CharField(max_length=50, blank=True, default='')
    related_object_id = models.UUIDField(null=True, blank=True)
    is_read = models.BooleanField(default=False)
    # Set by notifications_and_activity.tasks.send_notification_email_task --
    # the idempotency flag preventing a retried/duplicate task delivery from
    # sending the same email twice.
    email_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.type} -> {self.recipient_id}'
