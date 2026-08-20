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
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='assigned_projects', null=True)
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


class Attachment(UUIDModel):
    class ATTACHMENT_TYPE(models.TextChoices):
        FILE = 'file', 'File'
        LINK = 'link', 'Link'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='attachments')
    task = models.ForeignKey('Task', on_delete=models.CASCADE, related_name='attachments', null=True, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='uploaded_attachments', null=True)
    type = models.CharField(max_length=10, choices=ATTACHMENT_TYPE.choices, default=ATTACHMENT_TYPE.FILE)
    file = models.FileField(upload_to='project_documents/', blank=True, null=True)
    url = models.URLField(blank=True, null=True, default='')
    name = models.CharField(max_length=255, default='Untitled Attachment')
    label = models.CharField(max_length=255, blank=True, null=True, default='')
    content_type = models.CharField(max_length=100, blank=True, default='')
    size = models.PositiveIntegerField(null=True, blank=True, help_text='File size in bytes.')
    created_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} - {self.type}"
      
class TaskType(UUIDModel):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='task_types')

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
    spent_time = models.DurationField(blank=True, null=True)
    # Logical ordering only (e.g. AI-suggested sequence); not a dependency
    # graph -- V1 doesn't model task-to-task dependencies.
    sequence = models.PositiveIntegerField(default=0)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.title} - {self.project.title if self.project else 'No Project'} - {self.assigned_to.email if self.assigned_to else 'Unassigned'}"
