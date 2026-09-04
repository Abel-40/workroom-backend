"""Personal to-do lists.

Deliberately the most private thing in Workroom: a TodoItem belongs to
exactly one user and is never visible to anyone else -- not a teammate, not
a Department Leader, not the Company Owner. There is no sharing mechanism
here and none should be added (contrast pages.models.FolderShare, which
exists precisely because folders *are* shareable). Todos are also kept out
of CompanyActivity and analytics for the same reason.

A todo may optionally point at a Task the owner is assigned to, which is
what lets the AI service turn "what am I doing today" into a checklist. That
link is a convenience, not a grant: see todos.services for why the task's
title is snapshotted here and why the live link is hidden again if the task
is ever reassigned away.
"""

from django.conf import settings
from django.db import models
from utils.models import UUIDModel


class TodoItem(UUIDModel):
    class SOURCE(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        AI_GENERATED = 'ai_generated', 'AI Generated'

    # CASCADE, not SET_NULL: a todo with no owner is meaningless and would be
    # an orphaned private record nobody can reach or delete.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='todos')
    # The company the owner was working in when they made this. Todos are
    # personal, so this is not an access-control boundary -- `user` is -- but
    # it keeps the row scoped to one tenant for retention/export purposes and
    # leaves room for a user who later belongs to more than one company.
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='todos')
    task = models.ForeignKey(
        'projects_and_tasks.Task', on_delete=models.SET_NULL, null=True, blank=True, related_name='todos',
    )
    # Captured when the link is made, so the todo stays readable after the
    # task is renamed, archived, or unlinked -- and so a revoked link (the
    # owner is no longer the assignee) can still say what it was about
    # without exposing the task's current state.
    task_title_snapshot = models.CharField(max_length=255, blank=True, default='')

    title = models.CharField(max_length=255)
    notes = models.TextField(blank=True, default='')
    # A day, not a timestamp: the product requires the user to pick which day
    # a todo belongs to, and a time-of-day would make "today" ambiguous
    # across timezones for no benefit. Required -- there is no "someday" pile.
    due_date = models.DateField()
    # Manual ordering within a single day. Ties break on created_at.
    position = models.PositiveIntegerField(default=0)

    is_done = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=20, choices=SOURCE.choices, default=SOURCE.MANUAL)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        # Nearest first. Ascending due_date naturally floats overdue items to
        # the top (they hold the earliest dates), which is what the owner
        # needs to see first.
        ordering = ['due_date', 'position', 'created_at']
        indexes = [
            # The one query that matters: this user's open todos, nearest
            # first. Covers the list endpoint and the sidebar counts.
            models.Index(fields=['user', 'is_done', 'due_date'], name='todo_user_open_due_idx'),
        ]

    def __str__(self):
        return f'{self.title} ({self.due_date}) - {self.user_id}'
