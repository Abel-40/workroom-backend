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


class AIAssistantQuery(UUIDModel):
    """Lifecycle record for a single scoped-assistant question about a
    project (AI_SERVICE_SPEC.md scope: research/guidance about THIS project
    only, never a general-purpose assistant). Django owns this record
    end-to-end, same as AIGeneration -- the FastAPI service only ever
    returns an answer through Celery, never writes here directly.
    """

    class STATUS(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    project = models.ForeignKey(
        'projects_and_tasks.Project', on_delete=models.CASCADE, related_name='ai_assistant_queries',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='ai_assistant_queries',
    )
    question = models.TextField()
    reference_url = models.URLField(blank=True, default='')
    provider = models.CharField(max_length=50, blank=True, default='')
    model = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.PENDING)
    answer = models.TextField(blank=True, default='')
    # True when the assistant refused the question as out of scope (see the
    # OUT_OF_SCOPE: sentinel handled in ai_agent/tasks_assistant.py).
    refused = models.BooleanField(default=False)

    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(blank=True, default='')

    def __str__(self):
        return f"{self.project_id} - {self.status}"


class AIProjectHealthSummary(UUIDModel):
    """Lifecycle record for an on-demand, read-only natural-language summary
    of a project's current state. Generated from real, already-computed
    analytics only (analytics/services.py::get_project_stats) -- never
    claims to detect anything not derivable from those numbers."""

    class STATUS(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    class RISK_LEVEL(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'

    project = models.ForeignKey(
        'projects_and_tasks.Project', on_delete=models.CASCADE, related_name='ai_health_summaries',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='ai_health_summaries',
    )
    provider = models.CharField(max_length=50, blank=True, default='')
    model = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.PENDING)
    summary = models.TextField(blank=True, default='')
    risk_level = models.CharField(max_length=10, choices=RISK_LEVEL.choices, blank=True, default='')

    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(blank=True, default='')

    def __str__(self):
        return f"{self.project_id} - {self.status}"
