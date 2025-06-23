from django.urls import path,include
from rest_framework.routers import DefaultRouter
from .company_owners_api.views.user_views import AuthViewSet
from .company_owners_api.views.company_views import CompanyView
from .company_owners_api.views.task_type_views import TaskTypeView
from .company_owners_api.views.department_views import DepartmentView
from .company_owners_api.views.subscription_views import SubscriptionViewSet
from .company_owners_api.views.sector_views import SectorApiView
router = DefaultRouter()
router.register(r'auth',AuthViewSet,basename='auth')
router.register(r'company',CompanyView,basename='company')
router.register(r'default_task_type',TaskTypeView,basename='task_type')
router.register(r'department',DepartmentView,basename='department')
router.register(r'subscriptions', SubscriptionViewSet, basename='subscription')
router.register(r'sectors',SectorApiView,basename='sectors')
urlpatterns = [
  path('api/',include(router.urls))
  
]