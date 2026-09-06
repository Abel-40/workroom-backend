"""The Folder/Page ("wiki") system.

This is the one real, backend-persisted, permission-checked folder/page
system for Workroom -- used by both the Info Portal UI and the AI
Assistant's page-context/save-as-page features (DEVELOPMENT_RULES: no
duplicate folder/page systems). It replaces the previous Info Portal, which
was a frontend-only mock (Pinia state seeded in memory, persisted only to
localStorage, no API, no permission checks).
"""

from django.conf import settings
from django.db import models
from utils.models import UUIDModel


class PageFolder(UUIDModel):
    class COLOR(models.TextChoices):
        AMBER = 'amber', 'Amber'
        EMERALD = 'emerald', 'Emerald'
        CYAN = 'cyan', 'Cyan'
        VIOLET = 'violet', 'Violet'

    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='page_folders')
    name = models.CharField(max_length=255)
    color = models.CharField(max_length=10, choices=COLOR.choices, default=COLOR.AMBER)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='created_page_folders',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.company_id})"


class FolderShare(UUIDModel):
    """Grants one company member view/edit access to a folder they didn't
    create. Default visibility is creator-only (see pages/services.py) --
    this is the explicit exception, one row per (folder, user)."""

    folder = models.ForeignKey(PageFolder, on_delete=models.CASCADE, related_name='shares')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='shared_folders')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['folder', 'user'], name='unique_folder_share_per_user'),
        ]

    def __str__(self):
        return f"{self.folder_id} shared with {self.user_id}"


class Page(UUIDModel):
    """A rich-text page inside a PageFolder. ``blocks`` mirrors the
    frontend's existing InfoPageBlock shape exactly
    (heading/paragraph/list/attachment) so the former mock Info Portal store
    can move onto this API without a data-shape change. ``project`` is
    optional traceability only (e.g. a page saved from an AI Assistant
    answer about that project) -- folders/pages are company-scoped, not
    project-scoped."""

    folder = models.ForeignKey(PageFolder, on_delete=models.CASCADE, related_name='pages')
    project = models.ForeignKey(
        'projects_and_tasks.Project', on_delete=models.SET_NULL, null=True, blank=True, related_name='pages',
    )
    title = models.CharField(max_length=255, default='Untitled page')
    blocks = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='created_pages',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.title} ({self.folder_id})"
