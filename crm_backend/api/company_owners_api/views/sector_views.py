from rest_framework import viewsets,status
from company.models import Sector
from utils.api_response import api_response
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from ..serializers.sector_serializer import SectorSerializers
class SectorApiView(viewsets.ViewSet):
  
  @action(detail=False,methods=['get'],permission_classes=[AllowAny],authentication_classes=[])
  def get_all_sectors(self,request):
    sector = Sector.objects.all()
    serializer = SectorSerializers(sector,many=True)
    return api_response(
      message="sectors retrieved successfully",
      status_code=status.HTTP_200_OK,
      success=True,
      data={"sectors":serializer.data}
    )