from ..serializers.department_serializers import DefaultDepartmentSerializer,DDepartmentTypeSerializer,CreatedDepartmentTypeSerializer
from departments_and_teams.models import Department, DefaultDepartment
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from django.db.models import Q
from company.models import Company
from utils.api_response import api_response
from django.shortcuts import get_object_or_404
from company.models import Sector

class DepartmentView(viewsets.ViewSet):

    @action(detail=False, methods=['post'], permission_classes=[AllowAny], authentication_classes=[])
    def create_departments_from_defaults(self, request):
        serializer = DefaultDepartmentSerializer(data=request.data)

        if not serializer.is_valid():
            return api_response(
                message="Validation failed",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors=serializer.errors
            )

        selected_departments = serializer.validated_data.get('selected_types', [])
        use_all_default_departments = serializer.validated_data.get('use_all_default_departments', False)
        company_id = serializer.validated_data['company_id']

        # Get company
        try:
            company = Company.objects.select_related("owner", "sector").get(id=company_id)
        except Company.DoesNotExist:
            return api_response(
                message="Company does not exist.",
                status_code=status.HTTP_404_NOT_FOUND,
                success=False,
                errors={"company": ["Invalid company ID"]}
            )

        # Fetch default departments
        if use_all_default_departments:
            selected_departments = []
            departments = DefaultDepartment.objects.filter(
                Q(sector=company.sector) | Q(sector__isnull=True)
            )
        else:
            departments = DefaultDepartment.objects.filter(id__in=selected_departments)

        print("📌 Sector:", company.sector)
        print("📌 Selected Department IDs:", selected_departments)
        print("📌 Fetched Default Departments:", list(departments))

        # Check existing department names
        existing_departments = set(
            Department.objects.filter(company=company).values_list('name', flat=True)
        )
        existing_departments_lower = {name.lower() for name in existing_departments}

        new_departments = [
            Department(
                name=department.name,
                description=department.description,
                company=company
            )
            for department in departments
            if department.name.lower() not in existing_departments_lower
        ]

        print("📌 Existing Departments (lower):", existing_departments_lower)
        print("📌 Departments to Create:", [d.name for d in new_departments])

        Department.objects.bulk_create(new_departments)

        return api_response(
            message="Departments created successfully.",
            status_code=status.HTTP_201_CREATED,
            success=True,
            data={
                "company_id": company.id,
                "company_name": company.name,
                "sector": company.sector.name,
                "owner_email": company.owner.email,
                "created_departments":CreatedDepartmentTypeSerializer(departments,many=True).data,
                "total_departments_created": len(new_departments)
            }
        )
    @action(detail=True,methods=['get'],url_path='dept_types',permission_classes=[AllowAny])
    def get_default_dept_types(self,request,pk):
        sector = get_object_or_404(Sector,id=pk)
        defaultDeptTypes = DefaultDepartment.objects.filter(Q(sector=sector)| Q(sector__isnull=True))
        serializer = DDepartmentTypeSerializer(defaultDeptTypes,many=True)
        return api_response(
            message="Departments types retrieved successfully",
            status_code=status.HTTP_200_OK,
            success=True,
            data={"Department_types": serializer.data}
        )
