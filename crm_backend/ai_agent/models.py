from django.conf import settings
from django.db import models
from utils.models import UUIDModel


class AIGeneration(UUIDModel):
    """Lifecycle record for an AI project-decomposition request.

    Django owns this record end-to-end; the FastAPI AI service (Phase 6) only
    ever returns a structured plan back through Celery (Phase 7). It never
    writes to this table directly.
    """

    class STATUS(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    project = models.ForeignKey('projects_and_tasks.Project', on_delete=models.CASCADE, related_name='ai_generations')
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='ai_generations',
    )
    provider = models.CharField(max_length=50, blank=True, default='')
    model = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.PENDING)

    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    task_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default='')

    def __str__(self):
        return f"{self.project_id} - {self.status}"
