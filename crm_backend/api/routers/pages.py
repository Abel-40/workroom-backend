"""Folder/Page ("wiki") API. Company-scoped: any active company member can
list/create/edit -- see pages/services.py for why no per-folder ACL layer
exists. Used by both the Info Portal UI and the AI Assistant's page-context/
save-as-page features (one system, not two)."""

from uuid import UUID

from company.services import get_member_company
from ninja import Router
from pages import services
from pages.models import Page, PageFolder
from projects_and_tasks.services import get_viewable_project
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse, PageCreateIn, PageFolderCreateIn, PageUpdateIn

router = Router(tags=['pages'])
auth = JWTBearerAuth()


def folder_data(folder: PageFolder) -> dict:
    return {
        'id': str(folder.id), 'name': folder.name, 'color': folder.color,
        'created_at': folder.created_at.isoformat(),
    }


def page_data(page: Page, *, include_blocks: bool = True) -> dict:
    data = {
        'id': str(page.id), 'folder_id': str(page.folder_id),
        'project_id': str(page.project_id) if page.project_id else None,
        'title': page.title,
        'created_by': str(page.created_by_id) if page.created_by_id else None,
        'created_at': page.created_at.isoformat(), 'updated_at': page.updated_at.isoformat(),
    }
    if include_blocks:
        data['blocks'] = page.blocks
    return data


@router.get('/page-folders/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def list_folders(request):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    folders, _ = await services.list_folders(request.auth, company)
    return payload('Folders retrieved successfully.', 200, True, {'results': [folder_data(f) for f in folders]})


@router.post('/page-folders/', auth=auth, response={201: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def create_folder(request, data: PageFolderCreateIn):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    folder, error = await services.create_folder(request.auth, company, name=data.name, color=data.color)
    if error == 'forbidden':
        return payload('You do not have permission to create folders.', 403, False)
    return payload('Folder created successfully.', 201, True, {'folder': folder_data(folder)})


@router.get('/page-folders/{folder_id}/pages/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def list_pages_in_folder(request, folder_id: UUID, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    folder, error = await services.get_folder_for_user(request.auth, folder_id)
    if error == 'not_found':
        return payload('Folder not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this folder.', 403, False)
    queryset, _ = await services.list_pages(request.auth, folder)
    items, meta = await paginate(queryset, page, page_size)
    return payload('Pages retrieved successfully.', 200, True, {
        'results': [page_data(item, include_blocks=False) for item in items], 'meta': meta,
    })


@router.post('/page-folders/{folder_id}/pages/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def create_page_in_folder(request, folder_id: UUID, data: PageCreateIn):
    folder, error = await services.get_folder_for_user(request.auth, folder_id)
    if error == 'not_found':
        return payload('Folder not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to add pages to this folder.', 403, False)
    project = None
    if data.project_id:
        project, error = await get_viewable_project(request.auth, data.project_id)
        if error:
            return payload('Invalid project for this page.', 400, False, errors={'project_id': ['Invalid project']})
    page_obj, error = await services.create_page(request.auth, folder, title=data.title, blocks=data.blocks, project=project)
    if error == 'forbidden':
        return payload('You do not have permission to add pages to this folder.', 403, False)
    if error == 'invalid_project':
        return payload('The selected project is not in your company.', 400, False)
    return payload('Page created successfully.', 201, True, {'page': page_data(page_obj)})


@router.get('/pages/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def list_pages_for_picker(request, search: str = '', page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    queryset, _ = await services.list_pages_for_company(request.auth, company, search=search)
    items, meta = await paginate(queryset, page, page_size)
    results = []
    for item in items:
        data = page_data(item, include_blocks=False)
        data['folder_name'] = item.folder.name
        results.append(data)
    return payload('Pages retrieved successfully.', 200, True, {'results': results, 'meta': meta})


@router.get('/pages/{page_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def get_page(request, page_id: UUID):
    page_obj, error = await services.get_page_for_user(request.auth, page_id)
    if error == 'not_found':
        return payload('Page not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to view this page.', 403, False)
    return payload('Page retrieved successfully.', 200, True, {'page': page_data(page_obj)})


@router.patch('/pages/{page_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def update_page(request, page_id: UUID, data: PageUpdateIn):
    page_obj, error = await services.get_page_for_user(request.auth, page_id)
    if error == 'not_found':
        return payload('Page not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to edit this page.', 403, False)
    updated, error = await services.update_page(request.auth, page_obj, title=data.title, blocks=data.blocks)
    if error == 'forbidden':
        return payload('You do not have permission to edit this page.', 403, False)
    return payload('Page updated successfully.', 200, True, {'page': page_data(updated)})


@router.delete('/pages/{page_id}/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def delete_page(request, page_id: UUID):
    page_obj, error = await services.get_page_for_user(request.auth, page_id)
    if error == 'not_found':
        return payload('Page not found.', 404, False)
    if error == 'forbidden':
        return payload('You do not have permission to delete this page.', 403, False)
    if not await services.delete_page(request.auth, page_obj):
        return payload('You do not have permission to delete this page.', 403, False)
    return payload('Page deleted successfully.', 200, True)
