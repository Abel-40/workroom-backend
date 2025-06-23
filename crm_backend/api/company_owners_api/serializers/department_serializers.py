from rest_framework import serializers
from departments_and_teams.models import Department,DefaultDepartment

class DefaultDepartmentSerializer(serializers.Serializer):
    selected_types = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="List of selected DefaultDepartments IDs"
    )
    company_id = serializers.IntegerField()
    use_all_default_departments = serializers.BooleanField(default=False)
class DDepartmentTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = DefaultDepartment
        fields = ('id','name','sector','description')
        
        
class CreatedDepartmentTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ("name",)