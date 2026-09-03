from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
from users.models import User
from utils.models import UUIDModel

User = get_user_model()

class Project(UUIDModel):
    class STATUS(models.TextChoices):
        ACTIVE = 'Active', 'Active'
        INACTIVE = 'Inactive', 'Inactive'
        DONE = 'Done', 'Done'

    class PRIORITY(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'

    class VISIBILITY(models.TextChoices):
        PUBLIC = 'public', 'Public'
        COMPANY = 'company', 'Company'
        DEPARTMENT = 'department', 'Department'
        PRIVATE = 'private', 'Private'

    title = models.CharField(max_length=255, default='Untitled Project')
    description = models.TextField(default='No description provided')
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='projects')
    visibility = models.CharField(max_length=20, choices=VISIBILITY.choices, default=VISIBILITY.COMPANY)
    start_date = models.DateTimeField(default=timezone.now)
    deadline = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.ACTIVE)
    department = models.ForeignKey('departments_and_teams.Department', on_delete=models.CASCADE, related_name='projects', null=True)
    team = models.ForeignKey('departments_and_teams.Team',on_delete=models.CASCADE,related_name='project_team',null=True)
    # Immutable historical creator -- never reassigned, even after ownership
    # transfer or the creator leaving the company (SET_NULL, not CASCADE, so
    # this record survives a User row being removed).
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='assigned_projects', null=True)
    # The current responsible owner -- defaults to created_by at creation, but
    # is reassignable (see services.transfer_project_ownership) independently
    # of the immutable created_by above.
    current_owner = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='owned_projects', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    image = models.ImageField(upload_to='project_images/', blank=True, null=True)
    # A project's cover image is exactly one of an uploaded file (above) or an
    # external link (below) -- see projects_and_tasks.services for the
    # mutual-exclusion rule enforced whenever either is set.
    image_url = models.URLField(blank=True, default='')
    priority = models.CharField(max_length=20, choices=PRIORITY.choices, default=PRIORITY.MEDIUM)
    collaborators = models.ManyToManyField(User, related_name='collaborated_projects', blank=True)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.title} - {self.department.name if self.department else 'No Department'}"

    @property
    def total_tasks(self):
        return self.tasks.count()

    @property
    def active_tasks(self):
        return self.tasks.exclude(status=Task.STATUS.DONE).count()

    @property
    def completion_percent(self):
        total = self.tasks.count()
        if total == 0:
            return 0
        completed = self.tasks.filter(status=Task.STATUS.DONE).count()
        return (completed / total) * 100


class ProjectVisibilityRequest(UUIDModel):
    """A Department Member's request to raise a private project to
    department visibility -- see projects_and_tasks.services
    .request_visibility_change/approve_visibility_request/deny_visibility_request.
    Company-level visibility is deliberately out of a DM's reach through this
    workflow: only a Department Leader (or Owner/CM) may raise a project to
    company visibility, done directly via the ordinary project-update
    endpoint, not through a request/approval cycle."""

    class STATUS(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        DENIED = 'denied', 'Denied'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='visibility_requests')
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='visibility_requests', null=True)
    requested_visibility = models.CharField(max_length=20, choices=Project.VISIBILITY.choices)
    status = models.CharField(max_length=10, choices=STATUS.choices, default=STATUS.PENDING)
    decided_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, related_name='decided_visibility_requests', null=True, blank=True,
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['project'], condition=models.Q(status='pending'),
                name='one_pending_visibility_request_per_project',
            ),
        ]

    def __str__(self):
        return f"{self.project_id} -> {self.requested_visibility} ({self.status})"


class Attachment(UUIDModel):
    class ATTACHMENT_TYPE(models.TextChoices):
        FILE = 'file', 'File'
        LINK = 'link', 'Link'
        PAGE = 'page', 'Info Portal Page'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='attachments')
    task = models.ForeignKey('Task', on_delete=models.CASCADE, related_name='attachments', null=True, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='uploaded_attachments', null=True)
    type = models.CharField(max_length=10, choices=ATTACHMENT_TYPE.choices, default=ATTACHMENT_TYPE.FILE)
    file = models.FileField(upload_to='project_documents/', blank=True, null=True)
    url = models.URLField(blank=True, null=True, default='')
    # Info-Portal-page evidence (ATTACHMENT_TYPE.PAGE): references the page
    # directly instead of copying its content, so it always reflects the
    # page's current state at review time.
    page = models.ForeignKey('pages.Page', on_delete=models.SET_NULL, related_name='attachments', null=True, blank=True)
    # Scopes this attachment to one task-approval submission cycle -- null
    # for ordinary project/task documents, set only for evidence uploaded via
    # submit_task_for_approval. CASCADE: evidence has no purpose once its
    # approval cycle is deleted.
    approval = models.ForeignKey(
        'TaskApproval', on_delete=models.CASCADE, related_name='evidence', null=True, blank=True,
    )
    name = models.CharField(max_length=255, default='Untitled Attachment')
    label = models.CharField(max_length=255, blank=True, null=True, default='')
    content_type = models.CharField(max_length=100, blank=True, default='')
    size = models.PositiveIntegerField(null=True, blank=True, help_text='File size in bytes.')
    created_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} - {self.type}"


class TaskApproval(UUIDModel):
    """One evidence-submission review cycle for a task. A task may go through
    several of these over its lifetime (submit -> reject -> resubmit ->
    approve), so this is an append-only history, not a single mutable row
    per task -- see projects_and_tasks.services.submit_task_for_approval/
    approve_task/reject_task_approval."""

    class STATUS(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    task = models.ForeignKey('Task', on_delete=models.CASCADE, related_name='approvals')
    submitted_by = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='submitted_approvals', null=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=STATUS.choices, default=STATUS.PENDING)
    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='decided_approvals', null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    # Visible only to `submitted_by` -- enforced in the API serialization
    # layer (api.routers.tasks), never returned to the approver or anyone
    # else. See DEVELOPMENT_RULES on not leaking data via a shared read path.
    rejection_comment = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f"{self.task_id} - {self.status}"
      
class TaskType(UUIDModel):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='task_types')
    # Traceability back to the DefaultTaskType template this was created
    # from, if any -- see Department.default_department for the same
    # pattern/rationale. Null for manually-created task types and for any
    # row created before this field existed (no retroactive backfill).
    default_task_type = models.ForeignKey(
        'DefaultTaskType', on_delete=models.SET_NULL, null=True, blank=True, related_name='company_task_types',
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'name')  # Ensure same company doesn't duplicate names

    def __str__(self):
        return f"{self.name} ({self.company.name})"

class DefaultTaskType(UUIDModel):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    sector = models.ForeignKey(
        'company.Sector',
        on_delete=models.CASCADE,
        related_name='default_task_types',
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.name} ({self.sector.name if self.sector else 'All'})"



class Task(UUIDModel):
    class STATUS(models.TextChoices):
        TODO = 'To Do', 'To Do'
        IN_PROGRESS = 'In Progress', 'In Progress'
        IN_REVIEW = 'In Review', 'In Review'
        DONE = 'Done', 'Done'
    class PRIORITY(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'
    class SOURCE(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        AI_GENERATED = 'ai_generated', 'AI Generated'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='tasks', null=True)
    department = models.ForeignKey(
        'departments_and_teams.Department', on_delete=models.SET_NULL, related_name='tasks', null=True, blank=True,
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='created_tasks', null=True)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='tasks', null=True)
    title = models.CharField(max_length=255, default='Untitled Task')
    description = models.TextField(default='No description provided')
    deadline = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.TODO)
    source = models.CharField(max_length=20, choices=SOURCE.choices, default=SOURCE.MANUAL)
    task_type = models.ForeignKey(TaskType, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    priority = models.CharField(max_length=20, choices=PRIORITY.choices, default=PRIORITY.MEDIUM)
    estimated_time = models.DurationField(blank=True, null=True)
    # Logical ordering only (e.g. AI-suggested sequence); not a dependency
    # graph -- V1 doesn't model task-to-task dependencies.
    sequence = models.PositiveIntegerField(default=0)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.title} - {self.project.title if self.project else 'No Project'} - {self.assigned_to.email if self.assigned_to else 'Unassigned'}"


class TaskTimeLog(UUIDModel):
    """One real, attributable entry of work logged against a task -- replaces
    the old Task.spent_time single overwritable field, which had no history,
    no per-user attribution, and silently discarded the date/description a
    user entered (see TimeTrackingModal.vue). A task's total spent time is
    the live sum of its (non-deleted) entries -- see services.task_spent_hours
    -- rather than a cached column, so there's nothing to keep in sync."""
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='time_logs')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='task_time_logs')
    duration = models.DurationField()
    work_date = models.DateField(default=timezone.now)
    description = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.duration} on {self.task.title} by {self.user.email if self.user else 'Unknown'}"
