"""Django Ninja API backed by Pydantic request and response schemas."""

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.db.models import Q
from ninja import NinjaAPI
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
import stripe

from company.models import Company, Sector
from departments_and_teams.models import DefaultDepartment, Department
from plans.models import Plan
from projects_and_tasks.models import DefaultTaskType, TaskType
from subscriptions.models import Subscription
from users.models import CompanyUserProfile, PendingInvite
from utils.Invitation_email import send_invitation_email

from .auth import JWTBearerAuth
from .schemas import (
    AcceptInviteIn,
    ApiResponse,
    CheckoutIn,
    CompanyRegistrationIn,
    InviteIn,
    SelectionIn,
    SignInIn,
    SignUpIn,
)


api = NinjaAPI(
    title='Workroom API',
    version='1.0.0',
    description='Pydantic-validated API for the Workroom backend.',
    docs_url='/docs',
    openapi_url='/openapi.json',
)
auth = JWTBearerAuth()
User = get_user_model()


def payload(message: str, status_code: int = 200, success: bool = True, data=None, errors=None):
    """Keep the project response envelope while Ninja validates its schema."""
    body = {
        'success': success,
        'message': message,
        'statusCode': status_code,
        'data': data or {},
    }
    if errors:
        body['errors'] = errors
    return status_code, body


def user_data(user):
    return {
        'id': user.id,
        'email': user.email,
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
    }


@api.post('/auth/signup/', response={201: ApiResponse, 400: ApiResponse})
def signup(request, data: SignUpIn):
    if User.objects.filter(email__iexact=data.email).exists():
        return payload('Validation error', 400, False, errors={'email': ['This email is already registered.']})
    user = User.objects.create_user(
        email=data.email,
        username=data.username,
        password=data.password,
    )
    user.first_name = data.first_name
    user.last_name = data.last_name
    user.save(update_fields=['first_name', 'last_name'])
    return payload('User account created successfully', 201, True, {'user': user_data(user)})


@api.post('/auth/signin/', response={200: ApiResponse, 401: ApiResponse})
def signin(request, data: SignInIn):
    user = authenticate(request, username=data.email, password=data.password)
    if user is None:
        return payload('Invalid email or password.', 401, False)

    refresh = RefreshToken.for_user(user)
    response = JsonResponse({
        'success': True,
        'message': 'Login successful',
        'statusCode': 200,
        'data': {'user': user_data(user), 'is_authenticated': True, 'access': str(refresh.access_token)},
    }, status=200)
    response.set_cookie(
        key='refresh_token',
        value=str(refresh),
        httponly=True,
        secure=not settings.DEBUG,
        samesite='Lax',
        max_age=7 * 24 * 60 * 60,
    )
    return response


@api.post('/auth/refresh-token/', response={200: ApiResponse, 401: ApiResponse})
def refresh_token(request):
    token = request.COOKIES.get('refresh_token')
    if not token:
        return payload('No refresh token provided', 401, False)
    try:
        refresh = RefreshToken(token)
    except TokenError:
        return payload('Invalid refresh token', 401, False)
    return payload('Token refreshed successfully', 200, True, {'access': str(refresh.access_token)})


@api.post('/company/register/', response={201: ApiResponse, 400: ApiResponse, 404: ApiResponse})
def register_company(request, data: CompanyRegistrationIn):
    owner = User.objects.filter(id=data.owner).first()
    sector = Sector.objects.filter(id=data.sector).first()
    if owner is None:
        return payload('Owner user does not exist.', 404, False, errors={'owner': ['Invalid user ID']})
    if sector is None:
        return payload('Sector not found.', 404, False, errors={'sector': ['Invalid sector ID']})
    if Company.objects.filter(owner=owner).exists():
        return payload('User already has a company.', 400, False, errors={'owner': ['User already has a company.']})
    company = Company.objects.create(name=data.name, owner=owner, sector=sector)
    return payload('Company registered successfully.', 201, True, {
        'id': company.id, 'company_name': company.name, 'owner': owner.email, 'sector': sector.id,
    })


@api.get('/sectors/get_all_sectors/', response=ApiResponse)
def get_all_sectors(request):
    sectors = [{'id': item.id, 'name': item.name, 'description': item.description} for item in Sector.objects.all()]
    return payload('Sectors retrieved successfully', 200, True, {'sectors': sectors})


@api.post('/department/create_departments_from_defaults/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 403: ApiResponse, 404: ApiResponse})
def create_departments_from_defaults(request, data: SelectionIn):
    company = Company.objects.select_related('owner', 'sector').filter(id=data.company_id).first()
    if company is None:
        return payload('Company does not exist.', 404, False, errors={'company': ['Invalid company ID']})
    if company.owner_id != request.auth.id:
        return payload('You do not have permission to manage this company.', 403, False)
    departments = DefaultDepartment.objects.filter(
        Q(sector=company.sector) | Q(sector__isnull=True)
    ) if data.use_all_default_departments else DefaultDepartment.objects.filter(id__in=data.selected_types)
    existing_names = set(Department.objects.filter(company=company).values_list('name', flat=True))
    to_create = [Department(name=item.name, description=item.description, company=company) for item in departments if item.name not in existing_names]
    Department.objects.bulk_create(to_create)
    return payload('Departments created successfully.', 201, True, {
        'company_id': company.id,
        'created_departments': [{'name': item.name} for item in to_create],
        'total_departments_created': len(to_create),
    })


@api.get('/department/{sector_id}/dept_types/', response={200: ApiResponse, 404: ApiResponse})
def get_default_department_types(request, sector_id: int):
    sector = Sector.objects.filter(id=sector_id).first()
    if sector is None:
        return payload('Sector not found.', 404, False)
    departments = DefaultDepartment.objects.filter(Q(sector=sector) | Q(sector__isnull=True))
    return payload('Department types retrieved successfully', 200, True, {'department_types': [
        {'id': item.id, 'name': item.name, 'sector': item.sector_id, 'description': item.description} for item in departments
    ]})


@api.post('/default_task_type/default_task_type/', auth=auth, response={201: ApiResponse, 403: ApiResponse, 404: ApiResponse})
def create_task_types_from_defaults(request, data: SelectionIn):
    company = Company.objects.select_related('owner', 'sector').filter(id=data.company_id).first()
    if company is None:
        return payload('Company does not exist.', 404, False, errors={'company': ['Invalid company ID']})
    if company.owner_id != request.auth.id:
        return payload('You do not have permission to manage this company.', 403, False)
    task_types = DefaultTaskType.objects.filter(
        Q(sector=company.sector) | Q(sector__isnull=True)
    ) if data.use_all_default_task_types else DefaultTaskType.objects.filter(id__in=data.selected_types)
    existing_names = set(TaskType.objects.filter(company=company).values_list('name', flat=True))
    to_create = [TaskType(name=item.name, description=item.description, company=company) for item in task_types if item.name not in existing_names]
    TaskType.objects.bulk_create(to_create)
    return payload('Task types assigned successfully.', 201, True, {
        'company_id': company.id,
        'created_task_types': [{'name': item.name} for item in to_create],
        'total_task_types_created': len(to_create),
    })


@api.get('/default_task_type/{sector_id}/default-tasktypes/', response={200: ApiResponse, 404: ApiResponse})
def get_default_task_types(request, sector_id: int):
    sector = Sector.objects.filter(id=sector_id).first()
    if sector is None:
        return payload('Sector not found.', 404, False)
    task_types = DefaultTaskType.objects.filter(Q(sector=sector) | Q(sector__isnull=True))
    return payload('Task types retrieved successfully', 200, True, {'tasktypes': [
        {'id': item.id, 'name': item.name, 'sector': item.sector_id, 'description': item.description} for item in task_types
    ]})


@api.post('/auth/send_invite/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse})
def send_invite(request, data: InviteIn):
    profile = CompanyUserProfile.objects.filter(user=request.auth).select_related('company').first()
    company = Company.objects.filter(owner=request.auth).first() or (profile.company if profile else None)
    can_invite = Company.objects.filter(owner=request.auth).exists() or (profile and profile.role == CompanyUserProfile.Role.DEPARTMENT_LEADER)
    if not can_invite or company is None:
        return payload("You don't have permission to send invitations.", 403, False)
    if data.department and not Department.objects.filter(id=data.department, company=company).exists():
        return payload('Invalid department for this company.', 400, False)
    if CompanyUserProfile.objects.filter(user__email__iexact=data.email, company=company).exists():
        return payload('The user is already registered for this company.', 400, False)
    existing = PendingInvite.objects.filter(email__iexact=data.email, company=company, status=PendingInvite.Status.Pending).order_by('-created_at').first()
    if existing and not existing.is_expired():
        return payload('Invitation already sent for this email.', 400, False)
    invite = PendingInvite.objects.create(
        email=data.email, token=get_random_string(48), company=company, department_id=data.department, role=data.role,
    )
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://your-frontend.com').rstrip('/')
    send_invitation_email(data.email, request.auth.get_username(), company.name, f'{frontend_url}/invite/accept?token={invite.token}')
    return payload('Invitation sent successfully', 200, True, {'email': invite.email})


@api.post('/emp/accept_invite/', response={201: ApiResponse, 400: ApiResponse})
def accept_invite(request, data: AcceptInviteIn):
    invite = PendingInvite.objects.filter(token=data.token, status=PendingInvite.Status.Pending).first()
    if invite is None or invite.is_expired():
        if invite is not None:
            invite.status = PendingInvite.Status.Expired
            invite.save(update_fields=['status'])
        return payload('Invalid or expired invitation token.', 400, False)
    if User.objects.filter(email__iexact=invite.email).exists():
        return payload('An account with this email already exists.', 400, False)
    user = User.objects.create_user(email=invite.email, password=data.password, username=data.username)
    CompanyUserProfile.objects.create(user=user, company=invite.company, department=invite.department, role=invite.role)
    invite.status = PendingInvite.Status.Accepted
    invite.save(update_fields=['status'])
    return payload('User registered successfully.', 201, True, {'user': user_data(user)})


@api.post('/subscriptions/start-checkout/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 500: ApiResponse})
def start_checkout(request, data: CheckoutIn):
    plan = Plan.objects.filter(id=data.plan_id).first()
    company = Company.objects.filter(owner=request.auth).first()
    if plan is None or company is None:
        return payload('Invalid plan or company.', 400, False)
    if not plan.stripe_price_id:
        return payload('This plan is not configured for Stripe checkout.', 400, False)

    subscription, _ = Subscription.objects.get_or_create(company=company, defaults={'plan': plan})
    subscription.plan = plan
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        if not subscription.stripe_customer_id:
            customer = stripe.Customer.create(email=request.auth.email, name=company.name, metadata={'company_id': company.id})
            subscription.stripe_customer_id = customer.id
            subscription.save(update_fields=['plan', 'stripe_customer_id', 'updated_at'])
        else:
            customer = stripe.Customer.retrieve(subscription.stripe_customer_id)
        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
        session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=['card'],
            line_items=[{'price': plan.stripe_price_id, 'quantity': 1}],
            mode='subscription',
            success_url=f'{frontend_url}/success?session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=f'{frontend_url}/cancel',
        )
    except Exception:
        return payload('Stripe session creation failed.', 500, False)
    return payload('Checkout session created successfully.', 200, True, {'checkout_url': session.url})


@api.get('/subscriptions/my-subscription/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
def get_my_subscription(request):
    company = Company.objects.filter(owner=request.auth).first()
    subscription = getattr(company, 'subscription', None) if company else None
    if subscription is None:
        return payload('No subscription found.', 404, False)
    return payload('Subscription retrieved successfully.', 200, True, {
        'id': subscription.id, 'company': subscription.company_id, 'plan': subscription.plan_id,
        'status': subscription.status, 'is_trial': subscription.is_trial,
        'start_date': subscription.start_date.isoformat(),
        'current_period_end': subscription.current_period_end.isoformat() if subscription.current_period_end else None,
        'canceled_at': subscription.canceled_at.isoformat() if subscription.canceled_at else None,
        'is_active': subscription.is_active(), 'on_trial': subscription.on_trial(),
    })
