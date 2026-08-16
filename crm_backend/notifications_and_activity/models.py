from django.conf import settings
from django.db import models
from utils.models import UUIDModel


class Notification(UUIDModel):
    class Type(models.TextChoices):
        TASK_ASSIGNED = 'task_assigned', 'Task Assigned'
        TASK_COMPLETED = 'task_completed', 'Task Completed'
        INVITATION_ACCEPTED = 'invitation_accepted', 'Invitation Accepted'
        AI_GENERATION_COMPLETED = 'ai_generation_completed', 'AI Generation Completed'
        AI_GENERATION_FAILED = 'ai_generation_failed', 'AI Generation Failed'

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    type = models.CharField(max_length=30, choices=Type.choices)
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, default='')
    # Loose reference (no hard FK) to whatever this notification is about --
    # a project, task, company, or AI generation. Simpler than a
    # GenericForeignKey for what's currently a read-only, display-only link.
    related_object_type = models.CharField(max_length=50, blank=True, default='')
    related_object_id = models.UUIDField(null=True, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.type} -> {self.recipient_id}'
