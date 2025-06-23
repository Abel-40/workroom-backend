from rest_framework import serializers
from company.models import Sector

class SectorSerializers(serializers.ModelSerializer):
  class Meta:
    model = Sector
    fields = ('id','name','description')
    
