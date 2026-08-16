"""Async Django Ninja API with Pydantic schemas and async ORM access."""

import logging
from uuid import UUID

import stripe
from asgiref.sync import sync_to_async
from company.models import Company, Sector
from company.services import get_managed_company
from departments_and_teams.models import DefaultDepartment, Department
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from ninja import NinjaAPI
from plans.models import Plan
from projects_and_tasks.models import DefaultTaskType, TaskType
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from subscriptions.models import Subscription
from users.models import CompanyUserProfile, PendingInvite
from utils.api_response import api_response
from utils.Invitation_email import send_invitation_email
from utils.tokens import generate_token, hash_token

from .auth import JWTBearerAuth
from .routers.ai import router as ai_router
from .routers.documents import router as documents_router
from .routers.projects import router as projects_router
from .routers.tasks import router as tasks_router
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
    description='Asynchronous, Pydantic-validated API for the Workroom backend.',
    docs_url='/docs',
    openapi_url='/openapi.json',
)
auth = JWTBearerAuth()
User = get_user_model()

api.add_router('/projects', projects_router)
# Task, document, and AI routes mix '/projects/{id}/...' and '/tasks/{id}/...'
# style paths, so they mount at the API root and declare full paths themselves.
api.add_router('', tasks_router)
api.add_router('', documents_router)
api.add_router('', ai_router)

payload = api_response
logger = logging.getLogger(__name__)


def user_data(user):
    return {
        'id': user.id, 'email': user.email, 'username': user.username,
        'first_name': user.first_name, 'last_name': user.last_name,
    }


def accept_invite_in_transaction(token: str, password: str, username: str):
    """Transactions are sync-only in Django, so keep this multi-write flow atomic."""
    with transaction.atomic():
        invite = PendingInvite.objects.select_for_update().filter(
            token_hash=hash_token(token), status=PendingInvite.Status.Pending,
        ).first()
        if invite is None:
            return None, 'invalid'
        if invite.is_expired():
            invite.status = PendingInvite.Status.Expired
            invite.save(update_fields=['status'])
            return None, 'expired'
        if User.objects.filter(email__iexact=invite.email).exists():
            return None, 'existing_user'
        user = User.objects.create_user(email=invite.email, password=password, username=username)
        CompanyUserProfile.objects.create(
            user=user, company=invite.company, department=invite.department, role=invite.role,
        )
        invite.delete()
        return user, None


@api.post('/auth/signup/', response={201: ApiResponse, 400: ApiResponse})
async def signup(request, data: SignUpIn):
    if await User.objects.filter(email__iexact=data.email).aexists():
        return payload('Validation error', 400, False, errors={'email': ['This email is already registered.']})
    try:
        await sync_to_async(validate_password, thread_sensitive=True)(data.password)
    except DjangoValidationError as exc:
        return payload('Validation error', 400, False, errors={'password': exc.messages})
    user = await sync_to_async(User.objects.create_user, thread_sensitive=True)(
        email=data.email, username=data.username, password=data.password,
    )
    user.first_name, user.last_name = data.first_name, data.last_name
    await user.asave(update_fields=['first_name', 'last_name'])
    return payload('User account created successfully', 201, True, {'user': user_data(user)})


@api.post('/auth/signin/', response={200: ApiResponse, 401: ApiResponse})
async def signin(request, data: SignInIn):
    user = await sync_to_async(authenticate, thread_sensitive=True)(
        request, username=data.email, password=data.password,
    )
    if user is None:
        return payload('Invalid email or password.', 401, False)
    refresh = RefreshToken.for_user(user)
    response = JsonResponse({
        'success': True, 'message': 'Login successful', 'statusCode': 200,
        'data': {'user': user_data(user), 'is_authenticated': True, 'access': str(refresh.access_token)},
    }, status=200)
    response.set_cookie(
        key='refresh_token', value=str(refresh), httponly=True,
        secure=not settings.DEBUG, samesite='Lax', max_age=7 * 24 * 60 * 60,
    )
    return response


@api.post('/auth/refresh-token/', response={200: ApiResponse, 401: ApiResponse})
async def refresh_token(request):
    token = request.COOKIES.get('refresh_token')
    if not token:
        return payload('No refresh token provided', 401, False)
    try:
        refresh = RefreshToken(token)
    except TokenError:
        return payload('Invalid refresh token', 401, False)
    return payload('Token refreshed successfully', 200, True, {'access': str(refresh.access_token)})


@api.post('/company/register/', auth=auth, response={201: ApiResponse, 400: ApiResponse, 404: ApiResponse})
async def register_company(request, data: CompanyRegistrationIn):
    owner = request.auth
    sector = await Sector.objects.filter(id=data.sector).afirst()
    if sector is None:
        return payload('Sector not found.', 404, False, errors={'sector': ['Invalid sector ID']})
    if await Company.objects.filter(owner=owner).aexists():
        return payload('User already has a company.', 400, False, errors={'owner': ['User already has a company.']})
    company = await Company.objects.acreate(name=data.name, owner=owner, sector=sector)
    return payload('Company registered successfully.', 201, True, {
        'id': company.id, 'company_name': company.name, 'owner': owner.email, 'sector': sector.id,
    })


@api.get('/sectors/get_all_sectors/', response=ApiResponse)
async def get_all_sectors(request):
    sectors = [
        {'id': item.id, 'name': item.name, 'description': item.description}
        async for item in Sector.objects.all()
    ]
    return payload('Sectors retrieved successfully', 200, True, {'sectors': sectors})


@api.post('/department/create_departments_from_defaults/', auth=auth, response={201: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def create_departments_from_defaults(request, data: SelectionIn):
    company = await Company.objects.select_related('owner', 'sector').filter(id=data.company_id).afirst()
    if company is None:
        return payload('Company does not exist.', 404, False, errors={'company': ['Invalid company ID']})
    if company.owner_id != request.auth.id:
        return payload('You do not have permission to manage this company.', 403, False)
    defaults = DefaultDepartment.objects.filter(
        Q(sector=company.sector) | Q(sector__isnull=True)
    ) if data.use_all_default_departments else DefaultDepartment.objects.filter(id__in=data.selected_types)
    existing_names = {name async for name in Department.objects.filter(company=company).values_list('name', flat=True)}
    to_create = [
        Department(name=item.name, description=item.description, company=company)
        async for item in defaults if item.name not in existing_names
    ]
    if to_create:
        await Department.objects.abulk_create(to_create)
    return payload('Departments created successfully.', 201, True, {
        'company_id': company.id, 'created_departments': [{'name': item.name} for item in to_create],
        'total_departments_created': len(to_create),
    })


@api.get('/department/{sector_id}/dept_types/', response={200: ApiResponse, 404: ApiResponse})
async def get_default_department_types(request, sector_id: UUID):
    sector = await Sector.objects.filter(id=sector_id).afirst()
    if sector is None:
        return payload('Sector not found.', 404, False)
    departments = [
        {'id': item.id, 'name': item.name, 'sector': item.sector_id, 'description': item.description}
        async for item in DefaultDepartment.objects.filter(Q(sector=sector) | Q(sector__isnull=True))
    ]
    return payload('Department types retrieved successfully', 200, True, {'department_types': departments})


@api.post('/default_task_type/default_task_type/', auth=auth, response={201: ApiResponse, 403: ApiResponse, 404: ApiResponse})
async def create_task_types_from_defaults(request, data: SelectionIn):
    company = await Company.objects.select_related('owner', 'sector').filter(id=data.company_id).afirst()
    if company is None:
        return payload('Company does not exist.', 404, False, errors={'company': ['Invalid company ID']})
    if company.owner_id != request.auth.id:
        return payload('You do not have permission to manage this company.', 403, False)
    defaults = DefaultTaskType.objects.filter(
        Q(sector=company.sector) | Q(sector__isnull=True)
    ) if data.use_all_default_task_types else DefaultTaskType.objects.filter(id__in=data.selected_types)
    existing_names = {name async for name in TaskType.objects.filter(company=company).values_list('name', flat=True)}
    to_create = [
        TaskType(name=item.name, description=item.description, company=company)
        async for item in defaults if item.name not in existing_names
    ]
    if to_create:
        await TaskType.objects.abulk_create(to_create)
    return payload('Task types assigned successfully.', 201, True, {
        'company_id': company.id, 'created_task_types': [{'name': item.name} for item in to_create],
        'total_task_types_created': len(to_create),
    })


@api.get('/default_task_type/{sector_id}/default-tasktypes/', response={200: ApiResponse, 404: ApiResponse})
async def get_default_task_types(request, sector_id: UUID):
    sector = await Sector.objects.filter(id=sector_id).afirst()
    if sector is None:
        return payload('Sector not found.', 404, False)
    task_types = [
        {'id': item.id, 'name': item.name, 'sector': item.sector_id, 'description': item.description}
        async for item in DefaultTaskType.objects.filter(Q(sector=sector) | Q(sector__isnull=True))
    ]
    return payload('Task types retrieved successfully', 200, True, {'tasktypes': task_types})


@api.post('/auth/send_invite/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 403: ApiResponse})
async def send_invite(request, data: InviteIn):
    company = await get_managed_company(request.auth)
    if company is None:
        return payload("You don't have permission to send invitations.", 403, False)
    if data.department and not await Department.objects.filter(id=data.department, company=company).aexists():
        return payload('Invalid department for this company.', 400, False)
    if await CompanyUserProfile.objects.filter(user__email__iexact=data.email, company=company).aexists():
        return payload('The user is already registered for this company.', 400, False)
    existing = await PendingInvite.objects.filter(
        email__iexact=data.email, company=company, status=PendingInvite.Status.Pending,
    ).order_by('-created_at').afirst()
    if existing and not existing.is_expired():
        return payload('Invitation already sent for this email.', 400, False)
    raw_token = generate_token()
    invite = await PendingInvite.objects.acreate(
        email=data.email, token_hash=hash_token(raw_token), company=company,
        department_id=data.department, role=data.role,
    )
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://your-frontend.com').rstrip('/')
    try:
        await sync_to_async(send_invitation_email, thread_sensitive=True)(
            data.email, request.auth.get_username(), company.name,
            f'{frontend_url}/invite/accept?token={raw_token}',
        )
    except Exception:
        logger.exception('invite_email.send_failed', extra={'invite_id': str(invite.id)})
        return payload(
            'Invitation created, but the email could not be sent. It will be retried automatically.',
            200, True, {'email': invite.email, 'email_sent': False},
        )
    invite.email_sent = True
    await invite.asave(update_fields=['email_sent'])
    return payload('Invitation sent successfully', 200, True, {'email': invite.email, 'email_sent': True})


@api.post('/emp/accept_invite/', response={201: ApiResponse, 400: ApiResponse})
async def accept_invite(request, data: AcceptInviteIn):
    user, error = await sync_to_async(accept_invite_in_transaction, thread_sensitive=True)(
        data.token, data.password, data.username,
    )
    if error:
        message = 'An account with this email already exists.' if error == 'existing_user' else 'Invalid or expired invitation token.'
        return payload(message, 400, False)
    return payload('User registered successfully.', 201, True, {'user': user_data(user)})


@api.post('/subscriptions/start-checkout/', auth=auth, response={200: ApiResponse, 400: ApiResponse, 500: ApiResponse})
async def start_checkout(request, data: CheckoutIn):
    plan = await Plan.objects.filter(id=data.plan_id).afirst()
    company = await Company.objects.filter(owner=request.auth).afirst()
    if plan is None or company is None:
        return payload('Invalid plan or company.', 400, False)
    if not plan.stripe_price_id:
        return payload('This plan is not configured for Stripe checkout.', 400, False)
    subscription, _ = await Subscription.objects.aget_or_create(company=company, defaults={'plan': plan})
    if subscription.plan_id != plan.id:
        subscription.plan = plan
        await subscription.asave(update_fields=['plan', 'updated_at'])
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        if not subscription.stripe_customer_id:
            customer = await sync_to_async(stripe.Customer.create, thread_sensitive=False)(
                email=request.auth.email, name=company.name, metadata={'company_id': company.id},
            )
            subscription.stripe_customer_id = customer.id
            await subscription.asave(update_fields=['stripe_customer_id', 'updated_at'])
        else:
            customer = await sync_to_async(stripe.Customer.retrieve, thread_sensitive=False)(subscription.stripe_customer_id)
        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
        session = await sync_to_async(stripe.checkout.Session.create, thread_sensitive=False)(
            customer=customer.id, payment_method_types=['card'],
            line_items=[{'price': plan.stripe_price_id, 'quantity': 1}], mode='subscription',
            success_url=f'{frontend_url}/success?session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=f'{frontend_url}/cancel',
        )
    except Exception:
        return payload('Stripe session creation failed.', 500, False)
    return payload('Checkout session created successfully.', 200, True, {'checkout_url': session.url})


@api.get('/subscriptions/my-subscription/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def get_my_subscription(request):
    company = await Company.objects.filter(owner=request.auth).afirst()
    subscription = await Subscription.objects.filter(company=company).afirst() if company else None
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
