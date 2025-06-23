from django.contrib import admin
from .models import Department, Team,DefaultDepartment

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('id','name', 'company', 'leader', 'created_at')
    list_filter = ('company', 'created_at')
    search_fields = ('name', 'description')
    raw_id_fields = ('leader',)

@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('id','name', 'company', 'leader', 'created_at')
    list_filter = ('company', 'created_at')
    search_fields = ('name', 'description')
    raw_id_fields = ('leader', 'members')
    filter_horizontal = ('members',)

@admin.register(DefaultDepartment)
class DefalutDepartmentAdmin(admin.ModelAdmin):
    list_display = ('id','name', 'sector', 'description')
    list_filter = ('sector',)
    search_fields = ('name', 'description')