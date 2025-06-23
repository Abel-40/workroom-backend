from rest_framework.permissions import BasePermission
from users.models import CompanyUserProfile
class IsDepartmentLeader(BasePermission):

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        return request.user.CompanyUserProfile.role == CompanyUserProfile.Role.DEPARTMENT_LEADER
class IsLeaderOrAdmin(BasePermission):
  
  def has_permission(self, request, view):
      if not request.user.is_authenticated:
        return False
      
      return request.user.CompanyUserProfile.role in [CompanyUserProfile.Role.Owner, CompanyUserProfile.Role.DEPARTMENT_LEADER]