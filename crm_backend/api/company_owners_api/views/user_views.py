from rest_framework import viewsets,status
from ..serializers.user_serializers import UserSerializer,PendingInviteSerializer
from rest_framework.decorators import action
from users.models import PendingInvite
from utils.api_response import api_response
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from django.contrib.auth.hashers import check_password
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.utils.crypto import get_random_string
from rest_framework.decorators import action
from utils.Invitation_email import send_invitation_email
from company.models import Company
from django.shortcuts import get_object_or_404
from datetime import timedelta
from users.models import CompanyUserProfile
from company.models import Company
from django.utils import timezone

User = get_user_model()
class AuthViewSet(viewsets.ViewSet):

    @action(detail=False,methods=['post'],permission_classes=[AllowAny],authentication_classes=[])
    def signup(self,request):
        serializer = UserSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return api_response(
                    message="user account created successfully",
                    status_code=status.HTTP_201_CREATED,
                    success=True,
                    data={"user": serializer.data}
                )
        return api_response(
            message='Validation error',
            status_code=status.HTTP_400_BAD_REQUEST,
            success=False,
            errors=serializer.errors
         )

    @action(detail=False, methods=['post'], permission_classes=[AllowAny], authentication_classes=[])
    def signin(self, request):
        email = request.data.get('email')
        password = request.data.get('password')

        if not email or not password:
            return api_response(
                message="Email and password are required",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False
            )
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
                return api_response(
                    message="Invalid email.",
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    success=False
                )
        active_user = authenticate(request, username=email, password=password)
        if active_user is None:
            return api_response(
                message="Invalid password.",
                status_code=status.HTTP_401_UNAUTHORIZED,
                success=False                
            )
        seriaizer = UserSerializer(active_user)
        
        refresh = RefreshToken.for_user(active_user)
        response = api_response(
            message="Login successful",
            status_code=status.HTTP_200_OK,
            success=True,
            data={
                "user":seriaizer.data,
                "is_authenticated":user.is_authenticated,
                "access": str(refresh.access_token),
            }
        )
        response.set_cookie(
            key='refresh_token',
            value=str(refresh),
            httponly=True,
            secure=True,
            samesite='Strict',
        )
        return response

    @action(detail=False, methods=['post'], permission_classes=[AllowAny], authentication_classes=[])
    def refresh_token(self, request):
        refresh_token = request.COOKIES.get('refresh_token')
        
        if not refresh_token:
            return api_response(
                message="No refresh token provided",
                status_code=status.HTTP_401_UNAUTHORIZED,
                success=False
            )
        
        try:
            refresh = RefreshToken(refresh_token)
            access_token = str(refresh.access_token)

            response = api_response(
                message="Token refreshed successfully",
                status_code=status.HTTP_200_OK,
                success=True,
                data={"access": access_token}
            )

            
            response.set_cookie(
                key='refresh_token',
                value=str(refresh), 
                httponly=True,
                secure=True,
                samesite='Strict',
                max_age=7*24*60*60
            )

            return response

        except TokenError as e:
            return api_response(
                message="Invalid refresh token",
                status_code=status.HTTP_401_UNAUTHORIZED,
                success=False,
                errors={"token": str(e)}
            )

    @action(detail=False, methods=["post"])
    def send_invite(self, request):
        serializer = PendingInviteSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(
                message='Invitation Failed',
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors=serializer.errors
            )
        print(request.user)
        company_owner = Company.objects.filter(owner=request.user).first()
        dept_leader = CompanyUserProfile.objects.filter(user=request.user).first()

        # Check permission: must be either owner or department leader
        if not company_owner and not (dept_leader and dept_leader.role == CompanyUserProfile.Role.DEPARTMENT_LEADER):
            return api_response(
                message="Sorry, you don't have permission to send invitations.",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors={"error": "You don't have permission to add a new user."}
            )

        # Decide the company from whichever user type matched
        current_user_company = company_owner or (dept_leader.company if dept_leader else None)

        
        validated = serializer.validated_data
        email = validated['email']
        company = current_user_company
        department = validated.get('department')
        role = validated['role']
        if CompanyUserProfile.objects.filter(user__email=email).exists() :
            return api_response(
                message="the user is already registered for this company",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors={"error": "User with this email already exists in the company."}
            )

        if PendingInvite.objects.filter(email=email).exists():
            pendingEmployee = PendingInvite.objects.filter(email=email).order_by('created_at').first()
            print(pendingEmployee.is_expired())
            if pendingEmployee and not pendingEmployee.is_expired():
                return api_response(
                    message="Invitation already sent for this email.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    errors={"error": "Invitation already sent for this email and not expired yet."}
                )


            else :
                token = get_random_string(length=48)

                invite = PendingInvite.objects.create(
                    email=email,
                    token=token,
                    company=company,
                    department=department,
                    role=role,
                    status=PendingInvite.Status.Pending
                )

                invite_link = f"http://your-frontend.com/invite/accept?token={invite.token}"

                send_invitation_email(
                    email_recipient=email,
                    inviter_name=request.user.get_username() or request.user.username,
                    company_name=company.name,
                    invitation_link=invite_link
                )

                return api_response(
                    message="Invitation Sent Successfully",
                    status_code=status.HTTP_200_OK,
                    success=True,
                    data={"email": invite.email, "token": invite.token}
                )
        
        token = get_random_string(length=48)

        invite = PendingInvite.objects.create(
            email=email,
            token=token,
            company=company,
            department=department,
            role=role,
            status=PendingInvite.Status.Pending
        )

        invite_link = f"http://your-frontend.com/invite/accept?token={invite.token}"

        send_invitation_email(
            email_recipient=email,
            inviter_name=request.user.get_username() or request.user.username,
            company_name=company.name,
            invitation_link=invite_link
        )

        return api_response(
            message="Invitation Sent Successfully",
            status_code=status.HTTP_200_OK,
            success=True,
            data={"email": invite.email, "token": invite.token}
        )