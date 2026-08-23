from django.contrib import admin

from .models import Page, PageFolder


@admin.register(PageFolder)
class PageFolderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'company', 'color', 'created_by', 'created_at')
    list_filter = ('company', 'color')
    search_fields = ('name',)


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'folder', 'project', 'created_by', 'updated_at')
    list_filter = ('folder__company',)
    search_fields = ('title',)
    raw_id_fields = ('folder', 'project', 'created_by')
