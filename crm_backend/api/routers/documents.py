"""Project document upload/download API (Phase 4)."""

from uuid import UUID

from asgiref.sync import sync_to_async
from django.http import FileResponse
from ninja import File, Form, Router
from ninja.files import UploadedFile
from projects_and_tasks import services
from projects_and_tasks.models import Attachment
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['documents'])
auth = JWTBearerAuth()


def document_data(document: Attachment) -> dict:
    return {
        'id': str(document.id),
        'project_id': str(document.project_id),
        'task_id': str(document.task_id) if document.task_id else None,
        'uploaded_by': str(document.uploaded_by_id) if document.uploaded_by_id else None,
        'type': document.type,
        'name': document.name,
        'label': document.label,
        'content_type': document.content_type,
        'size': document.size,
        'created_at': document.created_at.isoformat(),
    }


@router.post('/projects/{project_id}/documents/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def upload_document(request, project_id: UUID, file: UploadedFile = File(...), label: str = Form(''), task_id: UUID | None = Form(None)):
    project, error = await services.get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to access this project.', 403, False)
    document, error = await services.upload_document(request.auth, project, file, label=label, task_id=task_id)
    if error == 'forbidden':
        return payload('You do not have permission to upload documents to this project.', 403, False)
    if error == 'too_large':
        return payload('File exceeds the maximum allowed size (10MB).', 400, False)
    if error == 'invalid_content_type':
        return payload('This file type is not allowed.', 400, False)
    if error == 'invalid_task':
        return payload('Invalid task for this project.', 400, False)
    return payload('Document uploaded successfully.', 201, True, {'document': document_data(document)})


@router.get('/projects/{project_id}/documents/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def list_documents(request, project_id: UUID, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    project, error = await services.get_viewable_project(request.auth, project_id)
    if error == 'not_found':
        return payload('Project not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to access this project.', 403, False)
    queryset = Attachment.objects.filter(project=project, is_deleted=False).order_by('-created_at')
    items, meta = await paginate(queryset, page, page_size)
    return payload('Documents retrieved successfully.', 200, True, {
        'results': [document_data(document) for document in items], 'meta': meta,
    })


@router.get('/documents/{document_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_document(request, document_id: UUID):
    document, error = await services.get_document_for_user(request.auth, document_id)
    if error == 'not_found':
        return payload('Document not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to access this document.', 403, False)
    return payload('Document retrieved successfully.', 200, True, {'document': document_data(document)})


@router.get('/documents/{document_id}/download/', auth=auth, response={403: ApiResponse, 404: ApiResponse})
async def download_document(request, document_id: UUID):
    document, error = await services.get_document_for_user(request.auth, document_id)
    if error == 'not_found':
        return payload('Document not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to access this document.', 403, False)
    if not document.file:
        return payload('This document has no downloadable file.', 404, False)
    file_handle = await sync_to_async(document.file.open, thread_sensitive=True)('rb')
    return FileResponse(
        file_handle, as_attachment=True, filename=document.name,
        content_type=document.content_type or 'application/octet-stream',
    )


@router.delete('/documents/{document_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def delete_document(request, document_id: UUID):
    document, error = await services.get_document_for_user(request.auth, document_id)
    if error == 'not_found':
        return payload('Document not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to access this document.', 403, False)
    if not await services.delete_document(request.auth, document):
        return payload('You do not have permission to delete this document.', 403, False)
    return payload('Document deleted successfully.', 200, True)
