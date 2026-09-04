"""Folder/Page ("wiki") system -- company-scoped AND creator-scoped.

Company membership is the outer tenant check (never trust a client-supplied
folder/page id without it). Within a company, a folder defaults to visible
only to the member who created it; :class:`~pages.models.FolderShare` is the
explicit exception -- share a folder with specific teammates to grant them
the same view/edit access the creator has (delete and re-sharing stay
creator-only). Every access point below re-derives this from the DB rather
than trusting a caller who already checked once, matching the rest of this
app's service layer.
"""

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db.models import Q

from company.services import is_company_member
from notifications_and_activity.services import notify_folder_shared
from users.models import CompanyUserProfile

from .models import FolderShare, Page, PageFolder

User = get_user_model()


def _folder_access_q(user, *, prefix: str = '') -> Q:
    """Q object for "created by this user OR shared with this user",
    filtered at the DB level rather than fetched-then-checked in Python.
    ``prefix`` lets the same Q reach through a FK, e.g. ``folder__`` when
    filtering Page instead of PageFolder directly."""
    return Q(**{f'{prefix}created_by': user}) | Q(**{f'{prefix}shares__user': user})


async def _can_access_folder(user, folder: PageFolder) -> bool:
    """Creator or explicitly shared-with. Assumes company membership has
    already been checked by the caller."""
    if folder.created_by_id == user.id:
        return True
    return await FolderShare.objects.filter(folder=folder, user=user).aexists()


async def list_folders(user, company):
    if not await is_company_member(user, company):
        return None, 'forbidden'
    folders = [
        f async for f in PageFolder.objects.filter(company=company, is_deleted=False)
        .filter(_folder_access_q(user))
        .distinct()
        .order_by('name')
    ]
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
    if not await _can_access_folder(user, folder):
        return None, 'forbidden'
    return folder, None


async def get_or_create_folder_by_name(user, company, name: str):
    """Only matches a folder ``user`` already has access to -- folders are
    creator-scoped now, so blindly matching any company folder with this
    name (even one belonging to someone else) would silently write into a
    folder the caller can't see, which is worse than just creating a new
    one."""
    folder = await PageFolder.objects.filter(
        company=company, name=name, is_deleted=False, created_by=user,
    ).afirst()
    if folder is not None:
        return folder, None
    return await create_folder(user, company, name=name)


async def share_folder(user, folder, target_user_ids: list) -> tuple[list | None, str | None]:
    """Only the folder's creator may share it, and only with active members
    of the same company -- a client-supplied user id is never trusted
    without that membership check."""
    if folder.created_by_id != user.id:
        return None, 'forbidden'
    if not target_user_ids:
        return [], None
    valid_ids = set()
    async for profile in CompanyUserProfile.objects.filter(
        company=folder.company, user_id__in=target_user_ids, is_active=True,
    ):
        valid_ids.add(profile.user_id)
    if folder.company.owner_id in target_user_ids:
        valid_ids.add(folder.company.owner_id)
    invalid_ids = [str(uid) for uid in target_user_ids if uid not in valid_ids]
    if invalid_ids:
        return None, 'invalid_members'
    shares = []
    for user_id in valid_ids:
        share, created = await FolderShare.objects.aget_or_create(folder=folder, user_id=user_id)
        shares.append(share)
        # Only on a genuinely new grant -- re-sharing with someone who
        # already has access must not re-notify them (Rule 8: repeating the
        # request produces no extra side effects).
        if created:
            recipient = await User.objects.filter(id=user_id).afirst()
            await sync_to_async(notify_folder_shared, thread_sensitive=True)(folder, recipient, user)
    return shares, None


async def list_folder_shares(user, folder):
    """Who currently has access to this folder. Readable by anyone who can
    already open the folder (creator or shared) -- they can see its contents,
    so who else can is not a further disclosure -- but only the creator may
    change the list (see share_folder/revoke_folder_share)."""
    if not await _can_access_folder(user, folder):
        return None, 'forbidden'
    shares = [
        s async for s in FolderShare.objects.filter(folder=folder)
        .select_related('user').order_by('created_at')
    ]
    return shares, None


async def revoke_folder_share(user, folder, target_user_id) -> tuple[bool, str | None]:
    """Removes one person's access. The creator may revoke anyone; a shared
    collaborator may revoke only themselves (leaving a folder they were
    added to). Revoking access nobody has is reported as 'not_found' rather
    than silently succeeding, so the caller can tell the two apart."""
    is_creator = folder.created_by_id == user.id
    if not is_creator and str(target_user_id) != str(user.id):
        return False, 'forbidden'
    deleted, _ = await FolderShare.objects.filter(folder=folder, user_id=target_user_id).adelete()
    if not deleted:
        return False, 'not_found'
    return True, None


async def list_pages(user, folder):
    """Returns an unevaluated queryset (not a materialized list) so the
    caller can paginate it -- matches the pattern used for other
    potentially-unbounded collections (e.g. api/routers/documents.py)."""
    if not await is_company_member(user, folder.company):
        return None, 'forbidden'
    if not await _can_access_folder(user, folder):
        return None, 'forbidden'
    return folder.pages.filter(is_deleted=False).order_by('-updated_at'), None


def _blocks_to_dicts(blocks) -> list:
    return [block.dict() if hasattr(block, 'dict') else dict(block) for block in (blocks or [])]


async def create_page(user, folder, *, title: str, blocks=None, project=None):
    if not await is_company_member(user, folder.company):
        return None, 'forbidden'
    if not await _can_access_folder(user, folder):
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
    if not await _can_access_folder(user, page.folder):
        return None, 'forbidden'
    return page, None


async def update_page(user, page, *, title=None, blocks=None):
    if not await is_company_member(user, page.folder.company):
        return None, 'forbidden'
    if not await _can_access_folder(user, page.folder):
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
    if not await _can_access_folder(user, page.folder):
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
    page-context lookup) only ever check the page's own is_deleted.

    Delete stays creator-only (unlike view/edit) -- a shared collaborator
    can work in a folder without being able to remove it out from under its
    owner."""
    if not await is_company_member(user, folder.company):
        return False
    if folder.created_by_id != user.id:
        return False
    folder.is_deleted = True
    await folder.asave(update_fields=['is_deleted'])
    await folder.pages.filter(is_deleted=False).aupdate(is_deleted=True)
    return True


async def list_pages_for_company(user, company, *, search: str = ''):
    """Flat, cross-folder page listing for the picker modal -- includes each
    page's folder via select_related so the picker can show/group by folder
    without N+1 lookups. Scoped to folders ``user`` can access (creator or
    shared), same as list_folders. Returns an unevaluated queryset for
    pagination, same as list_pages above."""
    if not await is_company_member(user, company):
        return None, 'forbidden'
    queryset = Page.objects.select_related('folder').filter(
        folder__company=company, is_deleted=False,
    ).filter(_folder_access_q(user, prefix='folder__')).distinct().order_by('-updated_at')
    if search:
        queryset = queryset.filter(title__icontains=search)
    return queryset, None


async def get_pages_by_ids_for_company(user, company, page_ids: list) -> list:
    """Returns only the pages among ``page_ids`` that actually belong to
    ``company``, aren't deleted, AND that ``user`` can access (creator or
    shared) -- callers must fail closed (reject the whole request) if the
    returned list is shorter than ``page_ids``: silently dropping an
    invalid/cross-tenant/no-access reference would look like it succeeded
    when it didn't (Rule 4)."""
    if not page_ids:
        return []
    return [
        p async for p in Page.objects.filter(
            id__in=page_ids, folder__company=company, is_deleted=False,
        ).filter(_folder_access_q(user, prefix='folder__')).distinct()
    ]


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
