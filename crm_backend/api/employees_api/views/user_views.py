from rest_framework import viewsets,status
from rest_framework.decorators import action
from users.models import PendingInvite
from utils.api_response import api_response
from rest_framework.permissions import AllowAny
from django.contrib.auth import get_user_model
from rest_framework.decorators import action
from company.models import Company
from users.models import CompanyUserProfile

User = get_user_model()
class AuthViewSet(viewsets.ViewSet):

  @action(detail=False, methods=["post"])
  def accept_invite(self, request):
      token = request.data.get('token')
      password = request.data.get('password')
      username = request.data.get('username')

      try:
          invite = PendingInvite.objects.get(token=token, status='pending')
      except PendingInvite.DoesNotExist:
            return api_response(
                message="Invalid refresh token",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors={"error": "Invalid or expired token."}
            )

      if invite.is_expired():
          invite.status = invite.Status.Expired
          invite.save()
          return  api_response(
                message="Invalid refresh token",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors={"error": "Invation Expired."}
            )
      user = User.objects.create_user(email=invite.email, password=password, username=username)
      
      CompanyUserProfile.objects.create(
          user=user,
          company=invite.company,
          department=invite.department,
          role=invite.role
      )

      invite.status = invite.Status.Accepted
      invite.save()

      return  api_response(
                message="Invalid refresh token",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                data=f"Successfully Registered"
            )
