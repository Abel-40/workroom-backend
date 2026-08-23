"""Company activity feed: a curated, read-only, company-wide event log
(project created/completed/ownership-transferred, member invited/joined/
removed, department/team created). Any company member may view it -- same
posture as analytics:view, since this is company-wide context rather than a
user-specific or permission-gated resource.
"""

from company.services import get_member_company
from ninja import Router
from notifications_and_activity.models import CompanyActivity
from utils.api_response import api_response as payload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['activity'])
auth = JWTBearerAuth()

MAX_LIMIT = 50
DEFAULT_LIMIT = 10


def _activity_data(item) -> dict:
    return {
        'id': str(item.id),
        'type': item.type,
        'summary': item.summary,
        'actor_id': str(item.actor_id) if item.actor_id else None,
        'actor_name': item.actor.username if item.actor_id else None,
        'related_object_type': item.related_object_type,
        'related_object_id': str(item.related_object_id) if item.related_object_id else None,
        'created_at': item.created_at.isoformat(),
    }


@router.get('/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def list_activity(request, limit: int = DEFAULT_LIMIT):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    bounded_limit = max(1, min(limit, MAX_LIMIT))
    items = [
        _activity_data(item)
        async for item in CompanyActivity.objects.filter(company=company)
        .select_related('actor')
        .order_by('-created_at')[:bounded_limit]
    ]
    return payload('Activity retrieved successfully.', 200, True, {'results': items})
