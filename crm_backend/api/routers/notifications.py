"""In-app notification list/read API (Phase 9)."""

from typing import Optional
from uuid import UUID

from ninja import Router
from notifications_and_activity.models import Notification
from utils.api_response import api_response as payload
from utils.pagination import DEFAULT_PAGE_SIZE, paginate

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['notifications'])
auth = JWTBearerAuth()


def notification_data(notification: Notification) -> dict:
    return {
        'id': str(notification.id),
        'type': notification.type,
        'title': notification.title,
        'message': notification.message,
        'related_object_type': notification.related_object_type,
        'related_object_id': str(notification.related_object_id) if notification.related_object_id else None,
        'is_read': notification.is_read,
        'created_at': notification.created_at.isoformat(),
    }


@router.get('/', auth=auth, response={200: ApiResponse})
async def list_notifications(request, is_read: Optional[bool] = None, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    # recipient=request.auth is the tenant boundary here: a user can only
    # ever see their own notifications, never another user's.
    queryset = Notification.objects.filter(recipient=request.auth)
    if is_read is not None:
        queryset = queryset.filter(is_read=is_read)
    items, meta = await paginate(queryset, page, page_size)
    unread_count = await Notification.objects.filter(recipient=request.auth, is_read=False).acount()
    return payload('Notifications retrieved successfully.', 200, True, {
        'results': [notification_data(n) for n in items], 'meta': meta, 'unread_count': unread_count,
    })


@router.post('/{notification_id}/read/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def mark_notification_read(request, notification_id: UUID):
    notification = await Notification.objects.filter(id=notification_id, recipient=request.auth).afirst()
    if notification is None:
        return payload('Notification not found.', 404, False)
    if not notification.is_read:
        notification.is_read = True
        await notification.asave(update_fields=['is_read'])
    return payload('Notification marked as read.', 200, True, {'notification': notification_data(notification)})


@router.post('/mark-all-read/', auth=auth, response={200: ApiResponse})
async def mark_all_notifications_read(request):
    updated = await Notification.objects.filter(recipient=request.auth, is_read=False).aupdate(is_read=True)
    return payload('Notifications marked as read.', 200, True, {'updated_count': updated})
