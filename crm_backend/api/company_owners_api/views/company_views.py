from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from company.models import Company, Sector
from projects_and_tasks.models import TaskType, DefaultTaskType 
from users.models import User
from ..serializers.company_serializers import CompanyRegisterationSerializer
from utils.api_response import api_response 
from rest_framework.decorators import action
from django.db.models import Q
from rest_framework.permissions import AllowAny
from departments_and_teams.models import DefaultDepartment,Department
class CompanyView(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    @action(detail=False,methods=['post'],permission_classes=[AllowAny],authentication_classes=[],url_path='register')
    def registration(self, request):
        serializer = CompanyRegisterationSerializer(data=request.data)

        if not serializer.is_valid():
            return api_response(
                message="Validation failed",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors=serializer.errors
            )

        name = serializer.validated_data['name']
        owner_id = serializer.validated_data['owner']
        sector_id = serializer.validated_data['sector']
        

        try:
            owner = User.objects.get(id=owner_id)
        except User.DoesNotExist:
            return api_response(
                message="Owner user does not exist.",
                status_code=status.HTTP_404_NOT_FOUND,
                success=False,
                errors={"owner": ["Invalid user ID"]}
            )

        try:
            sector = Sector.objects.get(id=sector_id)
        except Sector.DoesNotExist:
            return api_response(
                message="Sector not found.",
                status_code=status.HTTP_404_NOT_FOUND,
                success=False,
                errors={"sector": ["Invalid sector ID"]}
            )

        if Company.objects.filter(Q(owner=owner)).exists():
            return api_response(
                message="User already have a Company.",
                status_code=status.HTTP_404_NOT_FOUND,
                success=False,
                errors={"owner": ["User already have a company."]}
            )
        company = Company.objects.create(name=name, owner=owner, sector=sector)
        return api_response(
            message="Company registered successfully.",
            status_code=status.HTTP_201_CREATED,
            success=True,
            data={
                "id": company.id,
                "company_name": company.name,
                "owner": owner.email,
                "sector": sector.id
            }
        )
