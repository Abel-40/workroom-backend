"""Department directory API. Listing is read-only and tenant-scoped: any
company member can list their own company's departments (needed for
project/task creation dropdowns and the departments-management view),
matching the existing sector/task-type listing endpoints in api.api.
Creating a department requires "managed company" standing (owner or
department leader), the same check already used for sending invitations.
"""

from uuid import UUID

from asgiref.sync import sync_to_async
from company.services import get_member_company
from departments_and_teams import services
from departments_and_teams.models import Department
from django.db.models import Count
from ninja import Router, Schema
from pydantic import Field
from users import services as users_services
from users.models import CompanyUserProfile
from utils.api_response import api_response as payload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['departments'])
auth = JWTBearerAuth()


class DepartmentIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    leader_id: UUID | None = None


class DepartmentPatchIn(Schema):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class DepartmentLeaderIn(Schema):
    user_id: UUID


def _department_data(item) -> dict:
    return {
        'id': item.id, 'name': item.name, 'description': item.description,
        'leader_id': item.leader_id, 'leader_name': item.leader.username if item.leader_id else None,
        'member_count': item.member_count,
    }


@router.get('/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def list_departments(request):
    company = await get_member_company(request.auth)
    if company is None:
        return payload('You do not belong to a company.', 404, False)
    departments = [
        _department_data(item)
        async for item in Department.objects.filter(company=company)
        .select_related('leader')
        .annotate(member_count=Count('companyuserprofile', distinct=True))
        .order_by('name')
    ]
    return payload('Departments retrieved successfully.', 200, True, {'results': departments})


@router.post('/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse})
async def create_department(request, data: DepartmentIn):
    department, error = await services.create_department(
        request.auth, name=data.name, description=data.description, leader_id=data.leader_id,
    )
    if error == 'forbidden':
        return payload("You don't have permission to manage departments.", 403, False)
    if error == 'duplicate_name':
        return payload(
            'A department with this name already exists.', 400, False,
            errors={'name': ['Department name must be unique within your company']},
        )
    if error == 'invalid_leader':
        return payload(
            'Invalid leader for this company.', 400, False,
            errors={'leader_id': ['The selected leader is not a member of this company']},
        )
    department.member_count = 0
    return payload('Department created successfully.', 201, True, {'department': _department_data(department)})


async def _reload_with_member_count(department):
    department = await Department.objects.select_related('leader').aget(id=department.id)
    department.member_count = await CompanyUserProfile.objects.filter(department=department).acount()
    return department


@router.patch('/{department_id}/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def update_department(request, department_id: UUID, data: DepartmentPatchIn):
    department = await Department.objects.filter(id=department_id).afirst()
    if department is None:
        return payload('Department not found.', 404, False)
    updated, error = await services.update_department(request.auth, department, **data.model_dump(exclude_unset=True))
    if error == 'forbidden':
        return payload("You don't have permission to manage departments.", 403, False)
    if error == 'duplicate_name':
        return payload(
            'A department with this name already exists.', 400, False,
            errors={'name': ['Department name must be unique within your company']},
        )
    updated = await _reload_with_member_count(updated)
    return payload('Department updated successfully.', 200, True, {'department': _department_data(updated)})


@router.post('/{department_id}/leader/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def assign_department_leader(request, department_id: UUID, data: DepartmentLeaderIn):
    department = await Department.objects.filter(id=department_id).afirst()
    if department is None:
        return payload('Department not found.', 404, False)
    updated, error = await sync_to_async(users_services.set_department_leader, thread_sensitive=True)(
        request.auth, department, data.user_id,
    )
    if error == 'forbidden':
        return payload("You don't have permission to manage departments.", 403, False)
    if error == 'invalid_leader':
        return payload(
            'Invalid leader for this company.', 400, False,
            errors={'user_id': ['The selected leader is not a member of this company']},
        )
    updated = await _reload_with_member_count(updated)
    return payload('Department leader updated successfully.', 200, True, {'department': _department_data(updated)})


@router.delete('/{department_id}/leader/', auth=auth, response={200: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def revoke_department_leader(request, department_id: UUID):
    department = await Department.objects.filter(id=department_id).afirst()
    if department is None:
        return payload('Department not found.', 404, False)
    updated, error = await sync_to_async(users_services.revoke_department_leader, thread_sensitive=True)(
        request.auth, department,
    )
    if error == 'forbidden':
        return payload("You don't have permission to manage departments.", 403, False)
    updated = await _reload_with_member_count(updated)
    return payload('Department leader removed successfully.', 200, True, {'department': _department_data(updated)})
