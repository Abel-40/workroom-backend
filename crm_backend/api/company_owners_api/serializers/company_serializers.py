from company.models import Company,Sector
from rest_framework import serializers
from users.models import User

class CompanySerializer(serializers.Serializer):
  class meta:
    model = Company
    fields = ('name','created_at','owner','sector','plan','stripe_customer_id','stripe_subscription_id','subscription_status','is_trial','trial_end')

class CompanyRegisterationSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    owner = serializers.IntegerField()
    sector = serializers.IntegerField()



    def validate_owner(self, value):
        if not User.objects.filter(id=value).exists():
            raise serializers.ValidationError("Owner does not exist.")
        return value

    def validate_sector(self, value):
        if not Sector.objects.filter(id=value).exists():
            raise serializers.ValidationError("Sector does not exist.")
        return value


