from django.contrib import admin
from .models import Plan

@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ('id','name', 'price', 'max_departments', 'max_users', 'max_projects', 'max_tasks', 'trial_days')
    search_fields = ('name',)
    list_filter = ('price', 'trial_days')