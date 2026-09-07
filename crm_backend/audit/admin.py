from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """Read-only in the admin, by construction rather than by convention.

    An append-only table with an editable admin is not append-only. All three
    write permissions are refused, so the change form renders as a detail view
    and neither the delete action nor the delete button exists.
    """

    list_display = ('created_at', 'company', 'action', 'target_type', 'target_id', 'actor')
    list_filter = ('action', 'company')
    search_fields = ('target_id', 'request_id', 'reason')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
