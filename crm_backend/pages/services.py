"""Folder/Page ("wiki") system -- company-scoped, backend-enforced.

Access is company-membership-only, matching the Info Portal's existing
all-member positioning -- no per-folder ACL layer is built here
(ShareFolderModal.vue's per-member sharing stays a decorative frontend mock;
it was never load-bearing and isn't required for the AI Assistant to work).
"""

from company.services import is_company_member

from .models import Page, PageFolder


async def list_folders(user, company):
    if not await is_company_member(user, company):
        return None, 'forbidden'
    folders = [f async for f in PageFolder.objects.filter(company=company, is_deleted=False).order_by('name')]
    return folders, None


async def create_folder(user, company, *, name: str, color: str = PageFolder.COLOR.AMBER):
    if not await is_company_member(user, company):
        return None, 'forbidden'
    if color not in PageFolder.COLOR.values:
        color = PageFolder.COLOR.AMBER
    folder = await PageFolder.objects.acreate(company=company, name=name, color=color, created_by=user)
    return folder, None


async def get_folder_for_user(user, folder_id):
    folder = await PageFolder.objects.select_related('company').filter(id=folder_id, is_deleted=False).afirst()
    if folder is None:
        return None, 'not_found'
    if not await is_company_member(user, folder.company):
        return None, 'forbidden'
    return folder, None


async def get_or_create_folder_by_name(user, company, name: str):
    folder = await PageFolder.objects.filter(company=company, name=name, is_deleted=False).afirst()
    if folder is not None:
        return folder, None
    return await create_folder(user, company, name=name)


async def list_pages(user, folder):
    """Returns an unevaluated queryset (not a materialized list) so the
    caller can paginate it -- matches the pattern used for other
    potentially-unbounded collections (e.g. api/routers/documents.py)."""
    if not await is_company_member(user, folder.company):
        return None, 'forbidden'
    return folder.pages.filter(is_deleted=False).order_by('-updated_at'), None


def _blocks_to_dicts(blocks) -> list:
    return [block.dict() if hasattr(block, 'dict') else dict(block) for block in (blocks or [])]


async def create_page(user, folder, *, title: str, blocks=None, project=None):
    if not await is_company_member(user, folder.company):
        return None, 'forbidden'
    if project is not None and project.company_id != folder.company_id:
        return None, 'invalid_project'
    page = await Page.objects.acreate(
        folder=folder, title=title, blocks=_blocks_to_dicts(blocks), project=project, created_by=user,
    )
    return page, None


async def get_page_for_user(user, page_id):
    page = await Page.objects.select_related('folder', 'folder__company').filter(id=page_id, is_deleted=False).afirst()
    if page is None:
        return None, 'not_found'
    if not await is_company_member(user, page.folder.company):
        return None, 'forbidden'
    return page, None


async def update_page(user, page, *, title=None, blocks=None):
    if not await is_company_member(user, page.folder.company):
        return None, 'forbidden'
    update_fields = ['updated_at']
    if title is not None:
        page.title = title
        update_fields.append('title')
    if blocks is not None:
        page.blocks = _blocks_to_dicts(blocks)
        update_fields.append('blocks')
    await page.asave(update_fields=update_fields)
    return page, None


async def delete_page(user, page) -> bool:
    if not await is_company_member(user, page.folder.company):
        return False
    page.is_deleted = True
    await page.asave(update_fields=['is_deleted'])
    return True


async def delete_folder(user, folder) -> bool:
    """Soft-deletes the folder AND cascades to its pages. Cascading (rather
    than leaving pages with is_deleted=False under a deleted folder) is what
    keeps every existing Page query correct without also having to filter on
    folder__is_deleted everywhere -- e.g. list_pages_for_company and
    get_pages_by_ids_for_company (the cross-folder picker / AI Assistant
    page-context lookup) only ever check the page's own is_deleted."""
    if not await is_company_member(user, folder.company):
        return False
    folder.is_deleted = True
    await folder.asave(update_fields=['is_deleted'])
    await folder.pages.filter(is_deleted=False).aupdate(is_deleted=True)
    return True


async def list_pages_for_company(user, company, *, search: str = ''):
    """Flat, cross-folder page listing for the picker modal -- includes each
    page's folder via select_related so the picker can show/group by folder
    without N+1 lookups. Returns an unevaluated queryset for pagination, same
    as list_pages above."""
    if not await is_company_member(user, company):
        return None, 'forbidden'
    queryset = Page.objects.select_related('folder').filter(
        folder__company=company, is_deleted=False,
    ).order_by('-updated_at')
    if search:
        queryset = queryset.filter(title__icontains=search)
    return queryset, None


async def get_pages_by_ids_for_company(company, page_ids: list) -> list:
    """Returns only the pages among ``page_ids`` that actually belong to
    ``company`` and aren't deleted. Callers must fail closed (reject the
    whole request) if the returned list is shorter than ``page_ids`` --
    silently dropping an invalid/cross-tenant reference would look like it
    succeeded when it didn't (Rule 4)."""
    if not page_ids:
        return []
    return [p async for p in Page.objects.filter(id__in=page_ids, folder__company=company, is_deleted=False)]


def blocks_to_text(blocks: list, *, max_chars: int = 3000) -> str:
    """Flatten a page's structured blocks into plain text for AI context --
    mirrors projects_and_tasks.services.get_text_document_excerpts' role for
    text attachments, but a page's body is already structured JSON, not a
    file to read."""
    parts = []
    for block in blocks or []:
        block_type = block.get('type')
        if block_type in ('heading', 'paragraph') and block.get('text'):
            parts.append(block['text'])
        elif block_type == 'list' and block.get('items'):
            parts.extend(f"- {item}" for item in block['items'])
    return '\n'.join(parts)[:max_chars]
