from django.contrib import admin
from .models import DefaultEventType, Event, EventType


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'company', 'event_type', 'department', 'organizer', 'start_at')
    list_filter = ('event_type', 'department', 'is_recurring', 'is_deleted')
    search_fields = ('title', 'description', 'location')
    date_hierarchy = 'start_at'
    raw_id_fields = ('company', 'department', 'team', 'organizer', 'attendees')


@admin.register(EventType)
class EventTypeAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'company', 'created_at')
    list_filter = ('company', 'created_at')
    search_fields = ('name', 'description')


@admin.register(DefaultEventType)
class DefaultEventTypeAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'sector')
    list_filter = ('sector',)
    search_fields = ('name', 'description')
