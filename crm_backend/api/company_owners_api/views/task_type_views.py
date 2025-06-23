from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from django.db.models import Q
from company.models import Company,Sector
from ..serializers.task_type_serializers import DefaultTaskTypeSerializer, TaskType, DefaultTaskType,DtaskTypeSerializer,CreatedTaskTypeSerializer
from utils.api_response import api_response
from django.shortcuts import get_object_or_404


class TaskTypeView(viewsets.ViewSet):

    @action(detail=False,methods=['post'],permission_classes=[AllowAny],authentication_classes=[])
    def default_task_type(self, request):
        serializer = DefaultTaskTypeSerializer(data=request.data)

        if not serializer.is_valid():
            return api_response(
                message="Validation failed",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors=serializer.errors
            )

        selected_task_types = serializer.validated_data.get('selected_types', [])
        use_all_default_task_types = serializer.validated_data.get('use_all_default_task_types', False)
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

        # Fetch default task types
        if use_all_default_task_types:
            selected_task_types = []
            tasktypes = DefaultTaskType.objects.filter(
                Q(sector=company.sector) | Q(sector__isnull=True)
            )
        else:
            use_all_default_task_types = False
            tasktypes = DefaultTaskType.objects.filter(id__in=selected_task_types)


        existing_tasktypes = set(
          TaskType.objects.filter(company=company).values_list('name',flat=True)
        )
        new_taskType = [
          TaskType(name=task.name, description=task.description, company=company) for task in tasktypes if task.name not in existing_tasktypes
        ]
        TaskType.objects.bulk_create(new_taskType)

        return api_response(
            message="Task types assigned successfully.",
            status_code=status.HTTP_201_CREATED,
            success=True,
            data={
                "company_id": company.id,
                "company_name": company.name,
                "sector": company.sector.name,
                "owner_email": company.owner.email,
                "created_task_types":CreatedTaskTypeSerializer(tasktypes, many=True).data,
                "total_task_types": tasktypes.count()
            }
        )
        
    @action(
        detail=True,
        methods=['get'],url_path='default-tasktypes',permission_classes=[AllowAny],authentication_classes=[]
    )

    def get_default_tasktypes(self, request, pk):
        sector = get_object_or_404(Sector, id=pk)
        
        default_task_types = DefaultTaskType.objects.filter(Q(sector=sector) | Q(sector__isnull=True))
        serializer = DtaskTypeSerializer(default_task_types, many=True)
        
        return api_response(
            message="Task types retrieved successfully",
            status_code=status.HTTP_200_OK,
            success=True,
            data={"tasktypes": serializer.data}
        )
