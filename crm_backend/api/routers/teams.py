"""Team directory API. A team is a cross-department grouping of members
assembled for a specific project or initiative (unlike a Department, which
is a fixed org unit) -- see departments_and_teams.models.Team. Listing is
read-only and tenant-scoped like departments; creating one requires the
same "managed company" standing (owner or department leader).
"""

from uuid import UUID

from company.services import get_member_company
from departments_and_teams import services
from departments_and_teams.models import Team
from ninja import Router, Schema
from pydantic import Field
from utils.api_response import api_response as payload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['teams'])
auth = JWTBearerAuth()


class TeamIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    leader_id: UUID | None = None
    member_ids: list[UUID] = Field(default_factory=list)


async def _team_data(item) -> dict:
    member_ids = [str(user_id) async for user_id in item.members.values_list('id', flat=True)]
    return {
        'id': item.id, 'name': item.name, 'description': item.description,
        'leader_id': item.leader_id, 'leader_name': item.leader.username if item.leader_id else None,
        'member_ids': member_ids,
    }


@router.get('/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def list_teams(request):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    teams = [
        await _team_data(item)
        async for item in Team.objects.filter(company=company).select_related('leader').order_by('name')
    ]
    return payload('Teams retrieved successfully.', 200, True, {'results': teams})


@router.post('/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse})
async def create_team(request, data: TeamIn):
    team, error = await services.create_team(
        request.auth, name=data.name, description=data.description,
        leader_id=data.leader_id, member_ids=data.member_ids,
    )
    if error == 'forbidden':
        return payload("You don't have permission to manage teams.", 403, False)
    if error == 'duplicate_name':
        return payload(
            'A team with this name already exists.', 400, False,
            errors={'name': ['Team name must be unique within your company']},
        )
    if error == 'invalid_leader':
        return payload(
            'Invalid leader for this company.', 400, False,
            errors={'leader_id': ['The selected leader is not a member of this company']},
        )
    if error == 'invalid_member':
        return payload(
            'Invalid member for this company.', 400, False,
            errors={'member_ids': ['One or more users are not members of this company']},
        )
    return payload('Team created successfully.', 201, True, {'team': await _team_data(team)})
