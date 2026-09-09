"""Document access, validation, retention and storage accounting.

One function decides who may read a document, and one pair of constants
decides what may be uploaded. Both are shared by every upload path -- §7 asks
for content-type and size validation "on every upload path, not just approval
evidence", and the way to get that is to have one validator rather than four
copies that drift.
"""

from datetime import timedelta

from asgiref.sync import sync_to_async
from company.services import get_company_role, is_company_member
from django.db.models import Q, Sum
from django.utils import timezone
from entitlements import services as entitlements
from entitlements.models import UsageCounter
from projects_and_tasks.access import AccessLevel, resolve_project_access
from users.models import CompanyUserProfile

from .models import Document, DocumentShare

MAX_DOCUMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

ALLOWED_DOCUMENT_CONTENT_TYPES = {
    'application/pdf',
    'image/png', 'image/jpeg', 'image/gif', 'image/webp',
    'text/plain', 'text/csv',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/zip',
}

# How long a soft-deleted document stays recoverable before the purge job
# removes the file. Long enough that "I deleted the wrong thing" is a
# recoverable mistake over a weekend and a holiday, short enough that deleted
# data does not accumulate indefinitely against a company's storage.
RETENTION_DAYS = 30

COMPANY_MANAGE_ROLES = (CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.COMPANY_MANAGER)


def validate_upload(uploaded_file, *, max_bytes=None, allowed_types=None) -> str | None:
    """Returns 'empty_file', 'too_large', 'invalid_content_type', or None.

    The single validator §7 asks for -- "on every upload path, not just
    approval evidence". Every path that accepts a file calls this, so a new
    upload endpoint cannot ship without the checks by simply forgetting to
    copy them.

    The limits are arguments rather than fixed, because the differences
    between paths are real and worth keeping: a résumé and a profile picture
    have their own smaller caps and narrower type sets. What was not worth
    keeping was four hand-written copies of the same two `if` statements,
    each free to drift and one of them free to be missing.
    """
    max_bytes = MAX_DOCUMENT_SIZE_BYTES if max_bytes is None else max_bytes
    allowed_types = ALLOWED_DOCUMENT_CONTENT_TYPES if allowed_types is None else allowed_types

    if uploaded_file.size is None or uploaded_file.size <= 0:
        # A zero-byte upload is almost always a failed transfer rather than an
        # intended empty file, and storing it means somebody later opens
        # nothing and cannot tell why.
        return 'empty_file'
    if uploaded_file.size > max_bytes:
        return 'too_large'
    if (uploaded_file.content_type or '') not in allowed_types:
        return 'invalid_content_type'
    return None


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------

async def resolve_document_access(user, document) -> str | None:
    """What this user may do with this document: 'read', 'manage', or None.

    'manage' implies 'read' and adds deletion. One function for all four
    scopes, because the alternative -- a rule per scope, spread across the
    features that use them -- is how the personal scope ends up with an
    accidental administrative override that nobody notices until it matters.

    The scopes, and why each is what it is:

    ``personal``
        The owner, plus anyone explicitly shared with. **No administrative
        override of any kind**, in either direction: not Owner over CM, not CM
        over Owner, not a department leader over their own report. §7 is
        emphatic about this and it is the one rule here with no exceptions,
        so it is checked before anything else and returns immediately.
    ``project``
        Project VIEW reads. The uploader or project MANAGE deletes -- the
        person who filed it can take it back, and whoever shapes the project
        can clear it out.
    ``company``
        Any active member reads; Owner and Company Managers manage. This is
        the company handbook shelf, not a private drawer.
    ``folder``
        The Info Portal rule, unchanged: the folder's creator plus its
        FolderShare recipients.
    """
    if document.scope == Document.Scope.PERSONAL:
        # Deliberately first, and deliberately returning rather than falling
        # through to any company-role check below it.
        if document.owner_id == user.id:
            return 'manage'
        shared = await DocumentShare.objects.filter(document=document, user=user).aexists()
        return 'read' if shared else None

    if document.scope == Document.Scope.PROJECT:
        if document.project is None or document.project.is_deleted:
            # An archived project takes its documents with it. Without this a
            # document stays reachable by direct id after its project is gone
            # -- the same gap the old Attachment lookup closed with an
            # explicit project__is_deleted=False filter.
            return None
        access = await resolve_project_access(user, document.project)
        if access is None:
            return None
        if access >= AccessLevel.MANAGE or document.uploaded_by_id == user.id:
            return 'manage'
        return 'read'

    if document.scope == Document.Scope.COMPANY:
        if not await is_company_member(user, document.company):
            return None
        role = await get_company_role(user, document.company)
        return 'manage' if role in COMPANY_MANAGE_ROLES else 'read'

    if document.scope == Document.Scope.FOLDER:
        folder = document.folder
        if folder is None or folder.is_deleted:
            return None
        if not await is_company_member(user, document.company):
            return None
        if folder.created_by_id == user.id:
            return 'manage'
        from pages.models import FolderShare

        shared = await FolderShare.objects.filter(folder=folder, user=user).aexists()
        if not shared:
            return None
        return 'manage' if document.uploaded_by_id == user.id else 'read'

    return None


def documents_visible_to(user, company):
    """A queryset of the live documents this user may read in this company.

    Deliberately expressed as one queryset rather than as a filter applied
    after the fact: a listing that fetches everything and then drops what the
    caller may not see is one forgotten call away from being a leak, and it
    cannot be paginated correctly.
    """
    shared_ids = DocumentShare.objects.filter(user=user).values('document_id')
    from pages.models import FolderShare

    folder_ids = FolderShare.objects.filter(user=user).values('folder_id')

    return Document.objects.filter(
        Q(scope=Document.Scope.PERSONAL, owner=user)
        | Q(scope=Document.Scope.PERSONAL, id__in=shared_ids)
        | Q(scope=Document.Scope.COMPANY)
        | Q(scope=Document.Scope.FOLDER, folder__created_by=user, folder__is_deleted=False)
        | Q(scope=Document.Scope.FOLDER, folder_id__in=folder_ids, folder__is_deleted=False),
        company=company, is_deleted=False,
    )


# --------------------------------------------------------------------------
# Storage accounting
# --------------------------------------------------------------------------

async def company_storage_bytes(company) -> int:
    """Live bytes held by one company.

    Counts rows rather than asking the storage backend, so it stays cheap and
    stays correct against a backend that has its own eventual consistency.
    Soft-deleted documents are excluded: they are recoverable, but a company
    should not be charged for something they have already deleted.
    """
    total = await Document.objects.filter(
        company=company, is_deleted=False,
    ).aaggregate(total=Sum('size'))
    return total['total'] or 0


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

async def create_document(user, company, uploaded_file, *, scope, label='',
                          project=None, folder=None, task=None):
    """Store one document. Returns (document, error).

    Errors are the validate_upload codes plus 'forbidden'. The caller resolves
    the target (project, folder) and this checks whether the user may add to
    it -- upload authority differs per scope in a way reading does not:
    CONTRIBUTE uploads to a project, but only Owner/CM add company documents.
    """
    error = validate_upload(uploaded_file)
    if error:
        return None, error

    # Checked before the file is stored, not after -- accepting bytes and then
    # refusing them still costs the write.
    entitlement = await entitlements.check(
        company, UsageCounter.Metric.STORAGE_BYTES, requested=uploaded_file.size,
    )
    if not entitlement.allowed:
        return entitlement, 'storage_limit'

    if scope == Document.Scope.PROJECT:
        access = await resolve_project_access(user, project)
        if access is None or access < AccessLevel.CONTRIBUTE:
            return None, 'forbidden'
    elif scope == Document.Scope.COMPANY:
        role = await get_company_role(user, company)
        if role not in COMPANY_MANAGE_ROLES:
            return None, 'forbidden'
    elif scope == Document.Scope.FOLDER:
        from pages.models import FolderShare

        if folder is None or folder.is_deleted or folder.company_id != company.id:
            return None, 'forbidden'
        if folder.created_by_id != user.id and not await FolderShare.objects.filter(
            folder=folder, user=user,
        ).aexists():
            return None, 'forbidden'
    elif scope != Document.Scope.PERSONAL:
        return None, 'forbidden'

    document = await Document.objects.acreate(
        company=company, scope=scope,
        owner=user if scope == Document.Scope.PERSONAL else None,
        project=project if scope == Document.Scope.PROJECT else None,
        folder=folder if scope == Document.Scope.FOLDER else None,
        task=task if scope == Document.Scope.PROJECT else None,
        file=uploaded_file, name=uploaded_file.name[:255], label=label[:255],
        content_type=uploaded_file.content_type or '', size=uploaded_file.size,
        uploaded_by=user,
    )
    return document, None


async def delete_document(user, document):
    """Soft delete, starting the retention clock. Returns (ok, error)."""
    if await resolve_document_access(user, document) != 'manage':
        return False, 'forbidden'
    document.is_deleted = True
    document.deleted_at = timezone.now()
    document.deleted_by = user
    await document.asave(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])
    return True, None


async def restore_document(user, document):
    """Undo a soft delete inside the retention window. Returns (ok, error).

    The reason a retention window is worth having at all: without this, the
    window is just a delay before permanent loss, which helps nobody.
    """
    if not document.is_deleted:
        return False, 'not_deleted'
    if await resolve_document_access(user, document) != 'manage':
        return False, 'forbidden'
    document.is_deleted = False
    document.deleted_at = None
    document.deleted_by = None
    await document.asave(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])
    return True, None


def purge_expired_documents(*, now=None, retention_days=RETENTION_DAYS) -> int:
    """Permanently remove documents deleted longer ago than the window.

    Sync, because it deletes the stored file alongside the row and is only
    ever called from a Celery task. Returns how many were purged.

    Files are removed one at a time rather than through a queryset delete:
    a bulk delete would drop the rows and orphan every file behind them,
    which is the failure mode that makes storage accounting drift from
    reality.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(days=retention_days)
    expired = Document.objects.filter(is_deleted=True, deleted_at__lt=cutoff)

    purged = 0
    for document in expired.iterator():
        if document.file:
            document.file.delete(save=False)
        document.delete()
        purged += 1
    return purged


async def apurge_expired_documents(**kwargs) -> int:
    return await sync_to_async(purge_expired_documents, thread_sensitive=True)(**kwargs)
