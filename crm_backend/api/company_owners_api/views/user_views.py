from rest_framework import viewsets,status
from ..serializers.user_serializers import UserSerializer,PendingUserSerializer
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
        serializer = PendingUserSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(
                message='Inviation Failed',
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors=serializer.errors
            )
        email = request.data.get('email')
        company_id = request.data.get('company_id')
        department_id = request.data.get('department_id')
        role = request.data.get('role')

        if User.objects.filter(email=email).exists():
            return api_response(
                message="Invalid refresh token",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors={"error":"User with this email already exists"}
            )

        token = get_random_string(length=48)

        invite = PendingInvite.objects.create(
            email=email,
            token=token,
            company_id=company_id,
            department_id=department_id,
            role=role
        )

        invite_link = f"http://your-frontend.com/invite/accept?token={invite.token}"

        send_mail(
            subject="You're invited to join a company on Workroom",
            message=f"Click the link to join: {invite_link}",
            from_email="noreply@workroom.com",
            recipient_list=[email]
        )
        
        return api_response(
            message="Invalid refresh token",
            status_code=status.HTTP_400_BAD_REQUEST,
            success=False,
            data=f"Invitation sent successfully to {invite.email}."
        )
