from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
from users.models import User
from company.models import Company
User = get_user_model()

class Project(models.Model):
    class STATUS(models.TextChoices):
        ACTIVE = 'Active', 'Active'
        INACTIVE = 'Inactive', 'Inactive'
        DONE = 'Done', 'Done'

    class PRIORITY(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'

    title = models.CharField(max_length=255, default='Untitled Project')
    description = models.TextField(default='No description provided')
    deadline = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.ACTIVE)
    department = models.ForeignKey('departments_and_teams.Department', on_delete=models.CASCADE, related_name='projects', null=True)
    team = models.ForeignKey('departments_and_teams.Team',on_delete=models.CASCADE,related_name='project_team',null=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='assigned_projects', null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    image = models.ImageField(upload_to='project_images/', blank=True, null=True)
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


class Attachment(models.Model):
    class ATTACHMENT_TYPE(models.TextChoices):
        FILE = 'file', 'File'
        LINK = 'link', 'Link'

    task = models.ForeignKey('Task', on_delete=models.CASCADE, related_name='attachments', null=True, blank=True)
    type = models.CharField(max_length=10, choices=ATTACHMENT_TYPE.choices, default=ATTACHMENT_TYPE.FILE)
    file = models.FileField(upload_to='task_attachments/', blank=True, null=True)
    url = models.URLField(blank=True, null=True, default='')
    name = models.CharField(max_length=255, default='Untitled Attachment')
    label = models.CharField(max_length=255, blank=True, null=True, default='')

    def __str__(self):
        return f"{self.name} - {self.type}"
      
class TaskType(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='task_types')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'name')  # Ensure same company doesn't duplicate names

    def __str__(self):
        return f"{self.name} ({self.company.name})"

class DefaultTaskType(models.Model):
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



class Task(models.Model):
    class STATUS(models.TextChoices):
        TODO = 'To Do', 'To Do'
        IN_PROGRESS = 'In Progress', 'In Progress'
        IN_REVIEW = 'In Review', 'In Review'
        DONE = 'Done', 'Done'
    class PRIORITY(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='tasks', null=True)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='tasks', null=True)
    title = models.CharField(max_length=255, default='Untitled Task')
    description = models.TextField(default='No description provided')
    deadline = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS.choices, default=STATUS.TODO)
    task_type = models.ForeignKey(TaskType, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    priority = models.CharField(max_length=20, choices=PRIORITY.choices, default=PRIORITY.MEDIUM)
    estimated_time = models.DurationField(blank=True, null=True)
    spent_time = models.DurationField(blank=True, null=True)

    def __str__(self):
        return f"{self.title} - {self.project.title if self.project else 'No Project'} - {self.assigned_to.email if self.assigned_to else 'Unassigned'}"
