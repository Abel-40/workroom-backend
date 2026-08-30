"""Company member role management. Distinct from the read-only workload
listing at GET /analytics/company/members/ -- this router is the write side:
changing what role a member holds.

Authorization and tenant scoping are entirely delegated to
users.services.update_member_role, which re-derives the requester's managed
company and role from server-side state (never a client-supplied company
id) before touching anything, exactly like every other mutation in this API.
"""

import mimetypes
from datetime import date
from typing import Literal
from uuid import UUID

from asgiref.sync import sync_to_async
from django.http import FileResponse
from ninja import File, Router, Schema
from ninja.files import UploadedFile
from pydantic import Field
from users import services
from utils.api_response import api_response as payload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['members'])
auth = JWTBearerAuth()


class MemberRoleIn(Schema):
    """``role`` excludes 'Owner': a company has exactly one owner, and this
    endpoint can never mint or remove one (see users.services.update_member_role
    for the rest of the authorization -- Owner is also rejected as a target)."""

    role: Literal['CM', 'DL', 'DM']


class MemberStatusIn(Schema):
    is_active: bool


class MemberDepartmentIn(Schema):
    department_id: UUID | None = None


class MemberRemoveIn(Schema):
    reassign_to_user_id: UUID | None = None


class NotificationPreferenceIn(Schema):
    email_notifications_enabled: bool


class TimezonePreferenceIn(Schema):
    timezone: str


class MemberProfileUpdateIn(Schema):
    """All fields optional/partial -- see users.services.update_own_profile."""

    profession: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=200)
    phone_number: str | None = Field(default=None, max_length=20)
    birthday: date | None = None
    skype: str | None = Field(default=None, max_length=100)


def _member_data(profile) -> dict:
    return {
        'user_id': profile.user_id,
        'email': profile.user.email,
        'username': profile.user.username,
        'role': profile.role,
    }


def _member_detail_data(detail: dict) -> dict:
    return {
        'user_id': str(detail['user'].id),
        'email': detail['user'].email,
        'username': detail['user'].username,
        'first_name': detail['user'].first_name,
        'last_name': detail['user'].last_name,
        'role': detail['role'],
        'department_name': detail['department_name'],
        'profile_picture_url': detail['profile_picture_url'],
        'is_active': detail['is_active'],
        'email_notifications_enabled': detail['email_notifications_enabled'],
        'workload': detail['workload'],
    }


@router.patch(
    '/{user_id}/role/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def update_member_role(request, user_id: UUID, data: MemberRoleIn):
    profile, error = await sync_to_async(services.update_member_role, thread_sensitive=True)(
        request.auth, user_id, data.role,
    )
    if error == 'forbidden':
        return payload("You don't have permission to change this member's role.", 403, False)
    if error == 'cannot_change_self':
        return payload('You cannot change your own role.', 400, False)
    if error == 'invalid_target':
        return payload('Invalid member for this company.', 404, False)
    return payload('Member role updated successfully.', 200, True, {'member': _member_data(profile)})


@router.get(
    '/{user_id}/', auth=auth,
    response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def get_member_detail(request, user_id: UUID):
    detail, error = await services.get_member_detail(request.auth, user_id)
    if error == 'forbidden':
        return payload('You do not belong to a company.', 403, False)
    if error == 'not_found':
        return payload('Member not found.', 404, False)
    return payload('Member retrieved successfully.', 200, True, {'member': _member_detail_data(detail)})


@router.get(
    '/{user_id}/profile-image/', auth=auth,
    response={403: ApiResponse, 404: ApiResponse},
)
async def download_member_profile_image(request, user_id: UUID):
    """Stream an uploaded member picture after enforcing company membership.

    This mirrors the protected project-image route. It gives workload cards a
    usable image URL without publishing profile uploads through /media/.
    """
    profile, error = await services.get_member_profile_picture(request.auth, user_id)
    if error == 'forbidden':
        return payload('You do not belong to a company.', 403, False)
    if error == 'not_found':
        return payload('Profile image not found.', 404, False)
    content_type = mimetypes.guess_type(profile.profile_picture.name)[0] or 'application/octet-stream'
    file_handle = await sync_to_async(profile.profile_picture.open, thread_sensitive=True)('rb')
    return FileResponse(file_handle, content_type=content_type)


@router.patch(
    '/{user_id}/status/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def update_member_status(request, user_id: UUID, data: MemberStatusIn):
    profile, error = await sync_to_async(services.set_member_active_status, thread_sensitive=True)(
        request.auth, user_id, data.is_active,
    )
    if error == 'forbidden':
        return payload("You don't have permission to change this member's status.", 403, False)
    if error == 'cannot_change_self':
        return payload('You cannot change your own active status.', 400, False)
    if error == 'invalid_target':
        return payload('Invalid member for this company.', 404, False)
    return payload('Member status updated successfully.', 200, True, {'member': _member_data(profile)})


@router.patch(
    '/{user_id}/department/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def update_member_department(request, user_id: UUID, data: MemberDepartmentIn):
    profile, error = await sync_to_async(services.update_member_department, thread_sensitive=True)(
        request.auth, user_id, data.department_id,
    )
    if error == 'forbidden':
        return payload("You don't have permission to change this member's department.", 403, False)
    if error == 'invalid_target':
        return payload('Invalid member for this company.', 404, False)
    if error == 'invalid_department':
        return payload(
            'Invalid department for this company.', 400, False,
            errors={'department_id': ['Invalid department']},
        )
    return payload('Member department updated successfully.', 200, True, {'member': _member_data(profile)})


@router.post(
    '/{user_id}/remove/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse, 409: ApiResponse},
)
async def remove_member(request, user_id: UUID, data: MemberRemoveIn):
    result, error = await sync_to_async(services.remove_member, thread_sensitive=True)(
        request.auth, user_id, reassign_to_user_id=data.reassign_to_user_id,
    )
    if error == 'forbidden':
        return payload("You don't have permission to remove this member.", 403, False)
    if error == 'cannot_change_self':
        return payload('You cannot remove yourself.', 400, False)
    if error == 'invalid_target':
        return payload('Invalid member for this company.', 404, False)
    if error == 'invalid_reassignee':
        return payload(
            'Invalid reassignment target for this company.', 400, False,
            errors={'reassign_to_user_id': ['Must be a member of this company']},
        )
    if error == 'reassignment_required':
        return payload(
            'This member owns active projects or tasks that must be reassigned before removal.',
            409, False, result,
        )
    return payload('Member removed successfully.', 200, True, result)


@router.patch(
    '/me/notification-preference/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse},
)
async def update_notification_preference(request, data: NotificationPreferenceIn):
    profile, error = await services.update_notification_preference(request.auth, data.email_notifications_enabled)
    if error == 'forbidden':
        return payload('You do not belong to a company.', 400, False)
    if error == 'no_profile':
        return payload(
            'The company owner has no notification preference to update -- critical '
            'notifications are always delivered.', 400, False,
        )
    return payload(
        'Notification preference updated successfully.', 200, True,
        {'email_notifications_enabled': profile.email_notifications_enabled},
    )


@router.patch(
    '/me/timezone/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse},
)
async def update_timezone(request, data: TimezonePreferenceIn):
    user, error = await services.update_user_timezone(request.auth, data.timezone)
    if error == 'invalid_timezone':
        return payload(
            'Unrecognized timezone.', 400, False,
            errors={'timezone': ['Must be a valid IANA timezone name']},
        )
    return payload('Timezone updated successfully.', 200, True, {'timezone': user.timezone})


def _profile_fields_data(profile) -> dict:
    return {
        'profession': profile.profession,
        'address': profile.address,
        'phone_number': profile.phone_number,
        'birthday': profile.birthday.isoformat() if profile.birthday else None,
        'skype': profile.skype,
        'has_resume': bool(profile.resume),
    }


@router.patch(
    '/me/profile/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse},
)
async def update_own_profile(request, data: MemberProfileUpdateIn):
    profile, error = await services.update_own_profile(request.auth, data.model_dump(exclude_unset=True))
    if error == 'forbidden':
        return payload('You do not belong to a company.', 400, False)
    if error == 'no_profile':
        return payload('The company owner has no profile to update.', 400, False)
    return payload('Profile updated successfully.', 200, True, {'profile': _profile_fields_data(profile)})


@router.post(
    '/me/profile/resume/', auth=auth,
    response={200: ApiResponse, 400: ApiResponse},
)
async def upload_own_resume(request, resume: UploadedFile = File(...)):
    profile, error = await services.upload_own_resume(request.auth, resume)
    if error == 'forbidden':
        return payload('You do not belong to a company.', 400, False)
    if error == 'no_profile':
        return payload('The company owner has no profile to attach a resume to.', 400, False)
    if error == 'too_large':
        return payload('Resume exceeds the maximum allowed size (5MB).', 400, False)
    if error == 'invalid_content_type':
        return payload('Resume must be a PDF or Word document.', 400, False)
    return payload('Resume uploaded successfully.', 200, True, {'profile': _profile_fields_data(profile)})


@router.get(
    '/me/profile/resume/', auth=auth,
    response={400: ApiResponse, 404: ApiResponse},
)
async def download_own_resume(request):
    profile, error = await services.get_own_resume(request.auth)
    if error == 'forbidden':
        return payload('You do not belong to a company.', 400, False)
    if error == 'not_found':
        return payload('No resume has been uploaded.', 404, False)
    content_type = mimetypes.guess_type(profile.resume.name)[0] or 'application/octet-stream'
    file_handle = await sync_to_async(profile.resume.open, thread_sensitive=True)('rb')
    return FileResponse(
        file_handle, as_attachment=True, filename=profile.resume.name.rsplit('/', 1)[-1],
        content_type=content_type,
    )
