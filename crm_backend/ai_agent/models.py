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

    # Human-approved pool of eligible assignees the tasks may be
    # suggested-assigned to (validated via is_eligible_assignee before this
    # row was even created), and the hard cap on how many tasks the plan may
    # contain. Stored here (not just passed as Celery args) so the worker,
    # which only receives a generation id, can re-read both -- see
    # ai_agent/tasks.py::_build_request_payload/_store_generated_tasks_for_review.
    requested_assignee_ids = models.JSONField(default=list, blank=True)
    max_tasks = models.PositiveIntegerField(null=True, blank=True)

    # {opaque_ref: user_id} for exactly this generation. The AI never receives
    # a real user id and never returns one -- it works in refs, and this is
    # the only thing that can turn a ref back into a person. Regenerated per
    # generation so a ref leaked from one plan means nothing in another, and
    # so a ref cannot be correlated across projects.
    assignee_refs = models.JSONField(default=dict, blank=True)

    # Recorded on every generation from the day this ships, whether or not
    # anything bills on it yet: usage cannot be reconstructed retroactively,
    # and the first time somebody asks "what has this cost us" is far too
    # late to start counting.
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    # Provider-reported or computed at request time, in USD. Nullable because
    # "we do not know" and "it was free" are different answers.
    cost = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    # The provider actually used, when the primary failed and the fallback
    # answered. Null when the primary succeeded -- see ai_agent.providers.
    fallback_from = models.CharField(max_length=50, blank=True, default='')

    # Supplied by the client, scoped to the project. Two clicks on "Generate"
    # send the same key and produce one generation, which is the same
    # protection AITodoGeneration already has and for the same reason: a
    # double-click must not buy two provider calls.
    idempotency_key = models.CharField(max_length=64, blank=True, default='')

    # Set only once the generated plan has actually been persisted as real
    # backlog tasks (see projects_and_tasks.services.persist_ai_generated_tasks).
    # This, not `status`, is the authoritative "does this project already
    # have a saved AI plan" flag: a COMPLETED generation may still be an
    # unsaved draft awaiting review.
    saved_at = models.DateTimeField(null=True, blank=True)

    # Set when the requester abandons an unsaved draft (the "New plan" /
    # "discard" flow) so it stops being returned as this project's latest
    # generation -- without this, refreshing the page would resurrect a
    # draft the user explicitly discarded. Never set on a saved plan.
    discarded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # Two requests carrying the same key are one request, told twice.
            # A partial constraint -- most rows carry no key at all (an older
            # client, or one that never sends one), and those must not be
            # forced to collide with each other.
            models.UniqueConstraint(
                fields=['project', 'idempotency_key'],
                condition=~models.Q(idempotency_key=''),
                name='one_generation_per_project_idempotency_key',
            ),
        ]
        indexes = [
            # "does this project already have one in flight" -- checked on
            # every plan request.
            models.Index(fields=['project', 'status'], name='ai_gen_project_status_idx'),
        ]

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
    # The AI's suggestion only, from generation.requested_assignee_ids --
    # never trusted directly (Rule 10: never let the AI choose a user).
    # persist_ai_generated_tasks applies this to the real Task's assignee
    # only where a human hasn't already set `assigned_to` above and the
    # person is still eligible at save time.
    suggested_assignee = models.ForeignKey(
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

    # A reviewer has explicitly taken the AI's suggestion. Until this is True
    # the suggestion is shown and nothing else -- persist_ai_generated_tasks
    # will not apply it. V1 auto-applied every suggestion at save time, which
    # made "review the plan" mean "review the titles"; who does the work is
    # the decision least safe to make by default.
    suggested_assignee_accepted = models.BooleanField(default=False)
    # One line, from the model, saying why it suggested this person. Shown
    # beside the name so accepting is a judgement rather than a shrug.
    suggested_assignee_rationale = models.CharField(max_length=300, blank=True, default='')
    # The suggestion would put this person past a configured workload limit.
    # Flagged for the reviewer rather than dropped: the reviewer may know
    # something the numbers do not, and silently removing the suggestion would
    # hide the fact that it was ever made.
    suggested_assignee_over_capacity = models.BooleanField(default=False)

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

    **Private by default, and private means private.** `requested_by` is the
    only reader unless they explicitly share it. There is no project-manager
    override and no company-owner override, in either direction.

    That is stricter than it sounds and it is deliberate. What somebody asks
    an assistant is a record of what they did not know, and a manager able to
    read their reports' questions changes what people are willing to ask --
    which makes the feature worse for everybody, including the manager. The
    previous rule gated reads on project *view*, so every colleague who could
    open the project could read every question asked about it.
    """

    class STATUS(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    class Visibility(models.TextChoices):
        PRIVATE = 'private', 'Private to the person who asked'
        # Shared deliberately, by the owner, with everyone who can view the
        # project. There is no middle scope: a per-person share would be a
        # second permission system for one answer.
        PROJECT = 'project', 'Shared with the project'

    project = models.ForeignKey(
        'projects_and_tasks.Project', on_delete=models.CASCADE, related_name='ai_assistant_queries',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='ai_assistant_queries',
    )
    question = models.TextField()
    reference_url = models.URLField(blank=True, default='')
    # Defaults to private, which is also what every existing row becomes when
    # this column is added -- the migration needs no backfill because the
    # default *is* the correct historical answer.
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.PRIVATE)
    shared_at = models.DateTimeField(null=True, blank=True)
    # Workroom pages the requester explicitly selected as context -- distinct
    # from the project's text-attachment excerpts (get_text_document_excerpts),
    # which are always included regardless of selection.
    pages = models.ManyToManyField('pages.Page', blank=True, related_name='ai_assistant_queries')
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


class AITodoGeneration(UUIDModel):
    """Lifecycle record for turning a person's assigned work into their own
    private to-do list.

    Unlike every other record in this module, this one is owned by a *user*
    rather than a project: the todos it produces are private to the requester
    (todos/models.py), so `user` -- not a project's company scope -- is the
    access boundary for reading this row and everything it created.

    The AI service never picks which tasks are eligible. Django resolves the
    requester's own assigned tasks, stores their ids here, and re-checks the
    assignment again at persist time (see ai_agent/tasks_todos.py) -- a task
    can be reassigned while the generation is in flight.
    """

    class STATUS(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    class MODE(models.TextChoices):
        # Everything the requester is on the hook for, planned onto today.
        TODAY = 'today', 'Today'
        # One specific task they are assigned to, planned across a window.
        TASK = 'task', 'Task'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ai_todo_generations',
    )
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='ai_todo_generations')
    mode = models.CharField(max_length=10, choices=MODE.choices, default=MODE.TODAY)
    # Set only when mode=TASK, for traceability back to what was asked for.
    task = models.ForeignKey(
        'projects_and_tasks.Task', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ai_todo_generations',
    )
    # The exact task ids sent to the AI service. Stored here (not just passed
    # as Celery arguments) so the worker, which only receives a generation id,
    # can re-read them -- same reason AIGeneration stores requested_assignee_ids.
    source_task_ids = models.JSONField(default=list, blank=True)

    # Resolved in the requester's own timezone at request time, never by the
    # worker (which runs in UTC) and never by the AI service (which has no
    # clock). Every generated due date must land inside this range.
    window_start = models.DateField()
    window_end = models.DateField()

    instructions = models.TextField(blank=True, default='')
    max_todos = models.PositiveIntegerField(default=10)

    provider = models.CharField(max_length=50, blank=True, default='')
    model = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.PENDING)

    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    todo_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-requested_at']
        indexes = [
            # "Does this person already have one in flight?" -- checked on
            # every generate request to keep a double-click from burning two
            # provider calls.
            models.Index(fields=['user', 'status'], name='ai_todo_gen_user_status_idx'),
        ]

    def __str__(self):
        return f'{self.user_id} - {self.mode} - {self.status}'


class BriefAssist(UUIDModel):
    """Lifecycle record for the AI-assisted route into a project brief (§1).

    The guided form is the default way to fill in a ``ProjectBrief``. This is
    the other way: upload a document, let a model read it, and confirm what it
    understood -- **twice**, before any planning happens.

    The two confirmations are the whole point of the feature, so they are
    modelled as states rather than left to the client to remember::

        EXTRACTING -> EXTRACTED
                        |  confirm_extraction()   <- human gate 1
                        v
                   INTERPRETING -> INTERPRETED
                        |  confirm_interpretation()  <- human gate 2
                        v
                      READY   (and only now may a plan be generated)

    Nothing here writes to ``ProjectBrief``. Gate 1 does -- through the same
    validated ``update_brief`` path the guided form uses -- so an extraction
    nobody read cannot become project state, and a stale payload cannot become
    one either. That is the same rule ``ApprovalRequest`` follows for every
    other kind of approval in this codebase: approving applies the change
    through the path a direct action would take, never straight from stored
    JSON.

    A project may accumulate several of these over time (a second document, a
    restart after a failure); ``READY`` is a property of this attempt, and
    ``projects_and_tasks`` reads the most recent one.
    """

    class STATUS(models.TextChoices):
        EXTRACTING = 'extracting', 'Extracting'
        EXTRACTED = 'extracted', 'Extracted, awaiting confirmation'
        INTERPRETING = 'interpreting', 'Interpreting'
        INTERPRETED = 'interpreted', 'Interpreted, awaiting confirmation'
        READY = 'ready', 'Confirmed, ready to plan'
        FAILED = 'failed', 'Failed'

    project = models.ForeignKey(
        'projects_and_tasks.Project', on_delete=models.CASCADE, related_name='brief_assists',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='brief_assists',
    )
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.EXTRACTING)

    # The document's name only. The file itself is never stored here -- it is
    # read once, turned into text, and dropped. A brief is the artifact worth
    # keeping; the upload was a means to it, and storing it would quietly make
    # this a document store with none of documents.Document's retention rules.
    source_filename = models.CharField(max_length=255, blank=True, default='')
    source_chars = models.PositiveIntegerField(default=0)

    # What the model returned at each step, exactly as returned. Read by the
    # reviewer and never applied to anything directly -- see the class
    # docstring.
    extracted = models.JSONField(default=dict, blank=True)
    interpretation = models.JSONField(default=dict, blank=True)

    # Set when a human confirms each step. Timestamps rather than booleans:
    # "who agreed to this, and when" is the question somebody asks later, and
    # a boolean cannot answer it.
    extraction_confirmed_at = models.DateTimeField(null=True, blank=True)
    interpretation_confirmed_at = models.DateTimeField(null=True, blank=True)

    provider = models.CharField(max_length=50, blank=True, default='')
    model = models.CharField(max_length=100, blank=True, default='')
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)

    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # "the current attempt for this project" -- every read starts here.
            models.Index(fields=['project', '-created_at']),
        ]

    def __str__(self):
        return f'{self.project_id} - {self.status}'
