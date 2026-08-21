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
    # The requester's free-text description of what to build, sent to the AI
    # service as `requirements`. Stored here (not just passed to Celery as an
    # argument) so the Celery task can read it purely from generation_id.
    prompt = models.TextField(blank=True, default='')
    provider = models.CharField(max_length=50, blank=True, default='')
    model = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.PENDING)

    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    task_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default='')

    # Set only once the generated plan has actually been persisted as real
    # backlog tasks (see projects_and_tasks.services.persist_ai_generated_tasks).
    # This, not `status`, is the authoritative "does this project already
    # have a saved AI plan" flag: a COMPLETED generation may still be an
    # unsaved draft awaiting review.
    saved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.project_id} - {self.status}"


class AIGeneratedTask(UUIDModel):
    """One task proposed by a plan generation, held here for human review
    before (optionally) becoming a real Task. Structural fields (`sequence`,
    `dependency_temp_ids`, `temporary_id`) are set once at creation and never
    touched by a regeneration -- only content fields may be revised, and only
    for rows a reviewer has actually commented on."""

    class PRIORITY(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'

    generation = models.ForeignKey(AIGeneration, on_delete=models.CASCADE, related_name='generated_tasks')
    temporary_id = models.CharField(max_length=50)
    sequence = models.PositiveIntegerField()
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    priority = models.CharField(max_length=20, choices=PRIORITY.choices, default=PRIORITY.MEDIUM)
    estimated_effort = models.CharField(max_length=100, blank=True, default='')
    dependency_temp_ids = models.JSONField(default=list, blank=True)
    suggested_department = models.ForeignKey(
        'departments_and_teams.Department', on_delete=models.SET_NULL, null=True, blank=True,
    )
    suggested_task_type = models.ForeignKey(
        'projects_and_tasks.TaskType', on_delete=models.SET_NULL, null=True, blank=True,
    )
    # Set by a human reviewer only (see projects_and_tasks.services -- the AI
    # is never allowed to set this itself; GeneratedTask has no such field).
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    reviewer_comment = models.TextField(blank=True, default='')
    # False right after a comment is added/edited; flipped back to True once
    # a regeneration has incorporated it. Drives which rows "Regenerate Plan"
    # sends back to the AI service, and whether that action is offered at all.
    comment_resolved = models.BooleanField(default=True)
    created_task = models.OneToOneField(
        'projects_and_tasks.Task', on_delete=models.SET_NULL, null=True, blank=True, related_name='ai_generated_from',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sequence']
        unique_together = ('generation', 'temporary_id')

    def __str__(self):
        return f"{self.temporary_id} - {self.title}"


class AITaskContentRegeneration(UUIDModel):
    """Lifecycle record for regenerating one already-saved AI-generated
    task's description. Deliberately narrow: it only ever writes
    Task.description on completion, never creator/assignee/project/source --
    those simply aren't part of this record's job."""

    class STATUS(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    task = models.ForeignKey(
        'projects_and_tasks.Task', on_delete=models.CASCADE, related_name='ai_content_regenerations',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='ai_task_content_regenerations',
    )
    provider = models.CharField(max_length=50, blank=True, default='')
    model = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.PENDING)
    # What the user asked to change, if anything -- defaults to a generic
    # "improve this" instruction when they just click Regenerate with no
    # input (the AI service requires a non-empty instruction either way).
    instructions = models.TextField(blank=True, default='')
    previous_description = models.TextField(blank=True, default='')

    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(blank=True, default='')

    def __str__(self):
        return f"{self.task_id} - {self.status}"


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
