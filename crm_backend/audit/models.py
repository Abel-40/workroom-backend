"""Append-only audit trail.

Workroom now has three records of "something happened", and they answer three
different questions:

``CompanyActivity``
    A curated, company-wide feed. Answers "what is going on here lately". Any
    member may read it, it is deliberately incomplete, and it is written for
    people to skim.

``Notification``
    Per-recipient and permission-aware. Answers "what needs *your* attention".
    Delivered, read, dismissed.

``AuditEvent`` (this module)
    Append-only and complete for the actions it covers. Answers "who changed
    this, when, from what, to what, and why". Nobody reads it in the product
    yet -- there is no UI, no export, no retention policy. It exists now
    because it cannot be reconstructed retroactively: the day someone needs to
    know who moved a deadline, either the row was written at the time or the
    answer is gone.

Every row goes through :func:`audit.services.record_event`. That single
indirection is what lets a real domain-event bus replace this later without
touching a single call site.
"""

from django.conf import settings
from django.db import models
from utils.models import UUIDModel


class AuditAction(models.TextChoices):
    """The catalog of auditable actions.

    A closed set rather than free text: an audit trail whose action names drift
    (``project.deadline_changed`` vs ``project_deadline_change``) cannot be
    queried, and querying is the only reason it exists. Adding an action means
    adding a member here.
    """

    PROJECT_OWNERSHIP_TRANSFERRED = 'project.ownership_transferred', 'Project ownership transferred'
    PROJECT_DEADLINE_CHANGED = 'project.deadline_changed', 'Project deadline changed'
    PROJECT_VISIBILITY_CHANGED = 'project.visibility_changed', 'Project visibility changed'
    TASK_DEADLINE_CHANGED = 'task.deadline_changed', 'Task deadline changed'
    TASK_APPROVED = 'task.approved', 'Task approved'
    TASK_APPROVAL_REJECTED = 'task.approval_rejected', 'Task approval rejected'
    MEMBER_ROLE_CHANGED = 'member.role_changed', 'Member role changed'
    MEMBER_DEACTIVATED = 'member.deactivated', 'Member deactivated'
    MEMBER_REACTIVATED = 'member.reactivated', 'Member reactivated'
    MEMBER_REMOVED = 'member.removed', 'Member removed'
    COMPANY_SETTINGS_CHANGED = 'company.settings_changed', 'Company settings changed'


class AppendOnly(Exception):
    """Raised by any application code path that tries to change or remove an
    audit row. Deliberately not a ``ValidationError``: this is a programming
    error, not something a user did wrong, and it should surface as a 500 in
    development rather than be handled."""


class AuditEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AppendOnly('AuditEvent rows are append-only and cannot be updated.')

    def delete(self):
        raise AppendOnly('AuditEvent rows are append-only and cannot be deleted.')


class AuditEvent(UUIDModel):
    """One consequential change, recorded as it happened.

    Append-only is enforced on every application path: :meth:`save` refuses to
    write an existing row, :meth:`delete` refuses outright, and the manager's
    ``update()``/``delete()`` refuse too. The Django admin registration
    (:mod:`audit.admin`) grants no add, change or delete permission.

    The one deliberate exception is a cascade from ``Company``: Django's
    deletion collector issues its own SQL and does not go through the manager,
    so deleting a company still removes its audit rows. That is intended --
    a tenant's trail should not outlive the tenant, and a deletion request has
    to be satisfiable. No other path removes a row.

    There is deliberately no database-level trigger. The brief asks for
    append-only "in code or in Django admin", and a trigger would also block a
    future retention/purge job, which is a decision to take when retention is
    designed rather than one to foreclose here.
    """

    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='audit_events')
    # SET_NULL, not CASCADE: the record of what a since-deleted user did is
    # exactly what an audit trail is for.
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    action = models.CharField(max_length=64, choices=AuditAction.choices)
    # A loose reference (``app_label.modelname`` + pk) rather than a
    # GenericForeignKey: the target may since have been deleted, and the row
    # still has to make sense. Same convention as Notification.
    target_type = models.CharField(max_length=64)
    target_id = models.UUIDField()
    # Only the fields the action actually changed, coerced to JSON-safe
    # primitives. Never a full object dump -- see services._json_safe.
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    # Why the actor says they did it. Required by some callers (deadline
    # changes), blank for actions where the change speaks for itself.
    reason = models.TextField(blank=True, default='')
    # Correlates a row with the request's log lines (utils/middleware.py).
    request_id = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # "what happened in this company lately" -- the read every future
            # audit UI starts from.
            models.Index(fields=['company', '-created_at']),
            # "everything that ever happened to this project/task".
            models.Index(fields=['target_type', 'target_id', '-created_at']),
            # "every deadline change in this company" -- the compliance query.
            models.Index(fields=['company', 'action', '-created_at']),
        ]

    def __str__(self):
        return f'{self.action} on {self.target_type}:{self.target_id}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnly('AuditEvent rows are append-only and cannot be modified once written.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnly('AuditEvent rows are append-only and cannot be deleted.')
