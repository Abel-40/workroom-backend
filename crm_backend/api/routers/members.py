"""Company member role management. Distinct from the read-only workload
listing at GET /analytics/company/members/ -- this router is the write side:
changing what role a member holds.

Authorization and tenant scoping are entirely delegated to
users.services.update_member_role, which re-derives the requester's managed
company and role from server-side state (never a client-supplied company
id) before touching anything, exactly like every other mutation in this API.
"""

from typing import Literal
from uuid import UUID

from asgiref.sync import sync_to_async
from ninja import Router, Schema
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


def _member_data(profile) -> dict:
    return {
        'user_id': profile.user_id,
        'email': profile.user.email,
        'username': profile.user.username,
        'role': profile.role,
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
