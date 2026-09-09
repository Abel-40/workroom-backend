"""Unified document API.

One set of handlers for all four scopes. Which scope a document has decides
who may read it, and that decision lives in one place --
`documents.services.resolve_document_access` -- never in a handler here.

Personal documents are the strict case: no administrative override of any
kind, in either direction. No handler in this module consults a company role,
so there is nowhere for one to creep in.

The project-scoped URLs (`/projects/{id}/documents/`, `/documents/{id}/`,
`/documents/{id}/download/`) are unchanged from when project documents were
their own system. They are backed by `Document` now rather than `Attachment`;
the paths stayed because breaking every client to rename a model would be a
cost with no benefit to anyone.
"""

from uuid import UUID

from asgiref.sync import sync_to_async
from company.services import get_member_company
from django.http import FileResponse
from documents import services
from documents.models import Document, DocumentShare
from ninja import File, Form, Router, Schema
from ninja.files import UploadedFile
from pages.models import PageFolder
from projects_and_tasks import services as project_services
from projects_and_tasks.models import Task
from users.models import User
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['documents'])
auth = JWTBearerAuth()

UPLOAD_ERRORS = {
    'empty_file': 'That file is empty.',
    'too_large': 'File exceeds the maximum allowed size (10MB).',
    'invalid_content_type': 'This file type is not allowed.',
}


class DocumentShareIn(Schema):
    user_id: UUID


def _storage_message(entitlement) -> str:
    """Name the number rather than saying no. An upgrade prompt somebody can
    act on beats a 402 they have to guess at."""
    used_gb = entitlement.current / (1024 ** 3)
    limit_gb = (entitlement.limit or 0) / (1024 ** 3)
    return (
        f'Your plan includes {limit_gb:.0f} GB of storage and {used_gb:.2f} GB is in use. '
        'Delete something, or upgrade your plan.'
    )


def document_data(document: Document) -> dict:
    return {
        'id': str(document.id),
        'scope': document.scope,
        'project_id': str(document.project_id) if document.project_id else None,
        'task_id': str(document.task_id) if document.task_id else None,
        'folder_id': str(document.folder_id) if document.folder_id else None,
        'uploaded_by': str(document.uploaded_by_id) if document.uploaded_by_id else None,
        'name': document.name,
        'label': document.label,
        'content_type': document.content_type,
        'size': document.size,
        'created_at': document.created_at.isoformat(),
        'is_deleted': document.is_deleted,
        'deleted_at': document.deleted_at.isoformat() if document.deleted_at else None,
    }


async def _document_for_reading(user, document_id, *, include_deleted=False):
    """Returns (document, access) or (None, None).

    A caller who may not read a document gets the same answer as one asking
    about a document that does not exist -- a personal document must not
    confirm its own existence to somebody outside it.
    """
    queryset = Document.objects.select_related('company', 'project', 'project__company', 'folder', 'owner')
    if not include_deleted:
        queryset = queryset.filter(is_deleted=False)
    document = await queryset.filter(id=document_id).afirst()
    if document is None:
        return None, None
    access = await services.resolve_document_access(user, document)
    if access is None:
        return None, None
    return document, access


# --------------------------------------------------------------------------
# Project documents
# --------------------------------------------------------------------------

@router.post(
    '/projects/{project_id}/documents/', auth=auth,
    response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse, 402: ApiResponse, 404: ApiResponse},
)
async def upload_project_document(request, project_id: UUID, file: UploadedFile = File(...),
                                  label: str = Form(''), task_id: UUID | None = Form(None)):
    project, error = await project_services.get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to access this project.', 403, False)

    task = None
    if task_id is not None:
        task = await Task.objects.filter(id=task_id, project=project, is_deleted=False).afirst()
        if task is None:
            return payload('Invalid task for this project.', 400, False)

    document, error = await services.create_document(
        request.auth, project.company, file,
        scope=Document.Scope.PROJECT, label=label, project=project, task=task,
    )
    if error == 'forbidden':
        return payload('You do not have permission to upload documents to this project.', 403, False)
    if error == 'storage_limit':
        return payload(_storage_message(document), 402, False, errors={'plan': ['storage limit reached']})
    if error:
        return payload(UPLOAD_ERRORS.get(error, 'That file could not be accepted.'), 400, False)
    return payload('Document uploaded successfully.', 201, True, {'document': document_data(document)})


@router.get(
    '/projects/{project_id}/documents/', auth=auth,
    response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def list_project_documents(request, project_id: UUID, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    project, error = await project_services.get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to access this project.', 403, False)
    queryset = Document.objects.filter(
        scope=Document.Scope.PROJECT, project=project, is_deleted=False,
    ).order_by('-created_at')
    items, meta = await paginate(queryset, page, page_size)
    return payload('Documents retrieved successfully.', 200, True, {
        'results': [document_data(document) for document in items], 'meta': meta,
    })


# --------------------------------------------------------------------------
# Every scope
# --------------------------------------------------------------------------

@router.get('/documents/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def list_documents(request, scope: str | None = None, page: int = 1,
                         page_size: int = DEFAULT_PAGE_SIZE):
    """Everything the caller may read, filtered in the query rather than after
    it -- see services.documents_visible_to for why that distinction matters."""
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    queryset = services.documents_visible_to(request.auth, company)
    if scope:
        queryset = queryset.filter(scope=scope)
    items, meta = await paginate(queryset.order_by('-created_at'), page, page_size)
    return payload('Documents retrieved successfully.', 200, True, {
        'results': [document_data(document) for document in items], 'meta': meta,
    })


@router.post(
    '/documents/', auth=auth,
    response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse, 402: ApiResponse, 404: ApiResponse},
)
async def upload_document(
    request,
    file: UploadedFile = File(...),
    scope: str = Form(...),
    label: str = Form(''),
    folder_id: UUID | None = Form(None),
):
    """Personal, company and Info Portal folder documents. Project documents
    keep their own URL above, where the project is part of the path."""
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    if scope not in (Document.Scope.PERSONAL, Document.Scope.COMPANY, Document.Scope.FOLDER):
        return payload(
            'Use POST /projects/{id}/documents/ for project documents.', 400, False,
            errors={'scope': ['Must be personal, company, or folder']},
        )

    folder = None
    if scope == Document.Scope.FOLDER:
        folder = await PageFolder.objects.filter(id=folder_id, company=company, is_deleted=False).afirst()
        if folder is None:
            return payload('Folder not found.', 404, False)

    document, error = await services.create_document(
        request.auth, company, file, scope=scope, label=label, folder=folder,
    )
    if error == 'forbidden':
        return payload('You do not have permission to add a document here.', 403, False)
    if error == 'storage_limit':
        return payload(_storage_message(document), 402, False, errors={'plan': ['storage limit reached']})
    if error:
        return payload(UPLOAD_ERRORS.get(error, 'That file could not be accepted.'), 400, False)
    return payload('Document uploaded successfully.', 201, True, {'document': document_data(document)})


@router.get('/documents/storage/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def storage_usage(request):
    """Live bytes held by the caller's company, counted from rows rather than
    from the storage backend -- see services.company_storage_bytes.

    Registered before /documents/{document_id}/ because both are
    /documents/<segment>/ and the resolver takes the first pattern that
    matches, exactly as in the projects router.
    """
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    used = await services.company_storage_bytes(company)
    return payload('Storage usage retrieved successfully.', 200, True, {
        'used_bytes': used,
        'max_document_bytes': services.MAX_DOCUMENT_SIZE_BYTES,
        'retention_days': services.RETENTION_DAYS,
    })


@router.get('/documents/{document_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_document(request, document_id: UUID):
    document, _ = await _document_for_reading(request.auth, document_id)
    if document is None:
        return payload('Document not found.', 404, False)
    return payload('Document retrieved successfully.', 200, True, {'document': document_data(document)})


@router.get('/documents/{document_id}/download/', auth=auth, response={403: ApiResponse, 404: ApiResponse})
async def download_document(request, document_id: UUID):
    document, _ = await _document_for_reading(request.auth, document_id)
    if document is None:
        return payload('Document not found.', 404, False)
    if not document.file:
        return payload('This document has no downloadable file.', 404, False)
    file_handle = await sync_to_async(document.file.open, thread_sensitive=True)('rb')
    return FileResponse(
        file_handle, as_attachment=True, filename=document.name,
        content_type=document.content_type or 'application/octet-stream',
    )


@router.delete('/documents/{document_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def delete_document(request, document_id: UUID):
    """Soft delete. The file survives services.RETENTION_DAYS and can be
    restored until the purge job removes it."""
    document, _ = await _document_for_reading(request.auth, document_id)
    if document is None:
        return payload('Document not found.', 404, False)
    _, error = await services.delete_document(request.auth, document)
    if error == 'forbidden':
        return payload('You do not have permission to delete this document.', 403, False)
    return payload('Document deleted successfully.', 200, True)


@router.post(
    '/documents/{document_id}/restore/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def restore_document(request, document_id: UUID):
    document, _ = await _document_for_reading(request.auth, document_id, include_deleted=True)
    if document is None:
        return payload('Document not found.', 404, False)
    _, error = await services.restore_document(request.auth, document)
    if error == 'not_deleted':
        return payload('That document is not deleted.', 400, False)
    if error == 'forbidden':
        return payload('You do not have permission to restore this document.', 403, False)
    return payload('Document restored successfully.', 200, True, {'document': document_data(document)})


# --------------------------------------------------------------------------
# Sharing a personal document
# --------------------------------------------------------------------------

@router.post(
    '/documents/{document_id}/share/', auth=auth,
    response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse, 402: ApiResponse, 404: ApiResponse},
)
async def share_document(request, document_id: UUID, data: DocumentShareIn):
    """Personal documents only. Every other scope already has an audience, and
    a per-person grant on top of one would be a second, quieter permission
    system for the same thing."""
    document, _ = await _document_for_reading(request.auth, document_id)
    if document is None:
        return payload('Document not found.', 404, False)
    if document.scope != Document.Scope.PERSONAL:
        return payload(
            'Only personal documents are shared this way. Every other scope already has an audience.',
            400, False,
        )
    if document.owner_id != request.auth.id:
        # Owner, not 'manage': being shared with a document does not let you
        # pass it on. Only the owner decides who else sees it.
        return payload('Only the owner can share this document.', 403, False)

    company = await get_member_company(request.auth)
    recipient = await User.objects.filter(
        id=data.user_id, company_profiles__company=company, company_profiles__is_active=True,
    ).afirst()
    if recipient is None:
        return payload('That person is not a member of this company.', 404, False)
    if recipient.id == document.owner_id:
        return payload('That document already belongs to them.', 400, False)

    await DocumentShare.objects.aget_or_create(
        document=document, user=recipient, defaults={'shared_by': request.auth},
    )
    return payload('Document shared successfully.', 201, True)


@router.delete(
    '/documents/{document_id}/share/{user_id}/', auth=auth,
    response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def unshare_document(request, document_id: UUID, user_id: UUID):
    document, _ = await _document_for_reading(request.auth, document_id)
    if document is None:
        return payload('Document not found.', 404, False)
    if document.owner_id != request.auth.id:
        return payload('Only the owner can change who this document is shared with.', 403, False)
    await DocumentShare.objects.filter(document=document, user_id=user_id).adelete()
    return payload('Sharing removed successfully.', 200, True)
