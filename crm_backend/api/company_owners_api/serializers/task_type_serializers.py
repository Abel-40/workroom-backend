from rest_framework import serializers
from projects_and_tasks.models import TaskType,DefaultTaskType

class DefaultTaskTypeSerializer(serializers.Serializer):
    selected_types = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="List of selected DefaultTaskType IDs"
    )
    company_id = serializers.IntegerField()
    use_all_default_task_types = serializers.BooleanField(default=False)

class DtaskTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = DefaultTaskType
        fields = ('id','name','sector','description')
class CreatedTaskTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskType
        fields = ("name",)