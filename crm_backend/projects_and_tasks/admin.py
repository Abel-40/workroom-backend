from django.contrib import admin
from .models import Project, Task, TaskType, DefaultTaskType, Attachment

@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('id','title', 'status', 'priority', 'department', 'created_by', 'created_at', 'deadline')
    list_filter = ('status', 'priority', 'department', 'created_at')
    search_fields = ('title', 'description')
    date_hierarchy = 'created_at'
    raw_id_fields = ('department', 'team', 'created_by', 'collaborators')

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id','title', 'project', 'status', 'priority', 'assigned_to', 'deadline', 'created_at')
    list_filter = ('status', 'priority', 'created_at')
    search_fields = ('title', 'description')
    date_hierarchy = 'created_at'
    raw_id_fields = ('project', 'assigned_to', 'task_type')

@admin.register(TaskType)
class TaskTypeAdmin(admin.ModelAdmin):
    list_display = ('id','name', 'company', 'created_at')
    list_filter = ('company', 'created_at')
    search_fields = ('name', 'description')

@admin.register(DefaultTaskType)
class DefaultTaskTypeAdmin(admin.ModelAdmin):
    list_display = ('id','name', 'sector')
    list_filter = ('sector',)
    search_fields = ('name', 'description')

@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ('id','name', 'type', 'task', 'label')
    list_filter = ('type',)
    search_fields = ('name', 'label')
    raw_id_fields = ('task',)