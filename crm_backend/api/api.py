"""Async Django Ninja API with Pydantic schemas and async ORM access."""

import logging
from uuid import UUID

import stripe
from asgiref.sync import sync_to_async
from company.models import Company, Sector
from company.services import get_company_role, get_managed_company, get_member_company
from departments_and_teams import services as departments_and_teams_services
from departments_and_teams.models import DefaultDepartment, Department
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.http import JsonResponse
from ninja import File, Form, NinjaAPI
from ninja.errors import HttpError
from ninja.files import UploadedFile
from notifications_and_activity.services import log_member_invited, log_member_joined, notify_invitation_accepted
from permissions.catalog import has_permission
from plans.models import Plan
from projects_and_tasks import services as projects_and_tasks_services
from projects_and_tasks.models import DefaultTaskType
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from subscriptions.models import Subscription
from users.models import CompanyUserProfile, PendingInvite
from users.tasks import send_invite_email_task
from utils.api_response import api_response
from utils.rate_limit import rate_limit
from utils.tokens import generate_token, hash_token

from .auth import JWTBearerAuth
from .routers.activity import router as activity_router
from .routers.ai import router as ai_router
from .routers.company_config import router as company_config_router
from .routers.analytics import router as analytics_router
from .routers.departments import router as departments_router
from .routers.documents import router as documents_router
from .routers.members import router as members_router
from .routers.notifications import router as notifications_router
from .routers.pages import router as pages_router
from .routers.projects import router as projects_router
from .routers.task_types import router as task_types_router
from .routers.tasks import router as tasks_router
from .routers.teams import router as teams_router
from .schemas import (
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
api.add_router('', pages_router)
api.add_router('/notifications', notifications_router)
api.add_router('/analytics', analytics_router)
api.add_router('/departments', departments_router)
api.add_router('/teams', teams_router)
api.add_router('/task-types', task_types_router)
api.add_router('/company/members', members_router)
api.add_router('/activity', activity_router)
api.add_router('/company/default-config', company_config_router)

payload = api_response
logger = logging.getLogger(__name__)


@api.exception_handler(HttpError)
def handle_http_error(request, exc: HttpError):
    """Keep raised HttpErrors (e.g. utils.rate_limit's 429s) in the app's
    normal response envelope instead of Ninja's default {"detail": ...}."""
    return api.create_response(
        request, {'success': False, 'message': str(exc), 'statusCode': exc.status_code, 'data': {}},
        status=exc.status_code,
    )


def user_data(user):
    return {
        'id': user.id, 'email': user.email, 'username': user.username,
        'first_name': user.first_name, 'last_name': user.last_name,
        'timezone': user.timezone,
    }


def accept_invite_in_transaction(
    token: str,
    password: str,
    full_name: str,
    profession: str,
    phone_number: str,
    address: str,
    profile_picture: UploadedFile,
):
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
        first_name, _, last_name = full_name.strip().partition(' ')
        user = User.objects.create_user(
            email=invite.email,
            password=password,
            username=full_name.strip(),
        )
        user.first_name = first_name
        user.last_name = last_name
        user.save(update_fields=['first_name', 'last_name'])
        CompanyUserProfile.objects.create(
            user=user, company=invite.company, department=invite.department, role=invite.role,
            profession=profession.strip() or 'Not provided',
            phone_number=phone_number.strip() or 'Not provided',
            address=address.strip() or 'Not provided',
            profile_picture=profile_picture,
        )
        company = invite.company
        invite.delete()
        notify_invitation_accepted(user, company)
        log_member_joined(user, company)
        return user, None


@api.post('/auth/signup/', response={201: ApiResponse, 400: ApiResponse})
@rate_limit('signup', limit=10, window_seconds=3600)
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
@rate_limit('signin', limit=10, window_seconds=300)
async def signin(request, data: SignInIn):
    user = await sync_to_async(authenticate, thread_sensitive=True)(
        request, username=data.email, password=data.password,
    )
    if user is None:
        return payload('Invalid email or password.', 401, False)
    # Role/company context: not modeled on User itself (see users/models.py
    # CompanyUserProfile) -- the frontend needs it up front to tell an Owner
    # from a Department Leader/Member without a separate round trip.
    company = await get_member_company(user)
    role = await get_company_role(user, company) if company else None
    refresh = RefreshToken.for_user(user)
    response = JsonResponse({
        'success': True, 'message': 'Login successful', 'statusCode': 200,
        'data': {
            'user': user_data(user), 'is_authenticated': True, 'access': str(refresh.access_token),
            'role': role, 'company_id': str(company.id) if company else None,
            'company_name': company.name if company else None,
            'company_created_at': company.created_at.isoformat() if company else None,
        },
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


@api.post(
    '/department/create_departments_from_defaults/', auth=auth,
    response={201: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def create_departments_from_defaults(request, data: SelectionIn):
    company = await Company.objects.select_related('owner', 'sector').filter(id=data.company_id).afirst()
    if company is None:
        return payload('Company does not exist.', 404, False, errors={'company': ['Invalid company ID']})
    managed_company = await get_managed_company(request.auth)
    if managed_company is None or managed_company.id != company.id:
        return payload('You do not have permission to manage this company.', 403, False)
    to_create = await departments_and_teams_services.apply_default_departments(
        company, use_all=data.use_all_default_departments, selected_ids=data.selected_types,
    )
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


@api.post(
    '/default_task_type/default_task_type/', auth=auth,
    response={201: ApiResponse, 403: ApiResponse, 404: ApiResponse},
)
async def create_task_types_from_defaults(request, data: SelectionIn):
    company = await Company.objects.select_related('owner', 'sector').filter(id=data.company_id).afirst()
    if company is None:
        return payload('Company does not exist.', 404, False, errors={'company': ['Invalid company ID']})
    managed_company = await get_managed_company(request.auth)
    if managed_company is None or managed_company.id != company.id:
        return payload('You do not have permission to manage this company.', 403, False)
    to_create = await projects_and_tasks_services.apply_default_task_types(
        company, use_all=data.use_all_default_task_types, selected_ids=data.selected_types,
    )
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
@rate_limit('send_invite', limit=30, window_seconds=3600, key_func=lambda r: str(r.auth.id))
async def send_invite(request, data: InviteIn):
    company = await get_managed_company(request.auth)
    if company is None:
        return payload("You don't have permission to send invitations.", 403, False)
    if data.role == 'CM':
        requester_role = await get_company_role(request.auth, company)
        if not has_permission(requester_role, 'members:invite_cm'):
            return payload('Only the company owner can invite a Company Manager.', 403, False)
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
    # Email delivery runs on the Celery 'simple' queue, off the request
    # thread (Phase 9/11). The response can't know the outcome synchronously
    # in production -- email_sent reflects whatever's true immediately after
    # dispatch (still False in a real deployment; the task hasn't run yet).
    # thread_sensitive=True matters here: under CELERY_TASK_ALWAYS_EAGER
    # (tests), .delay() runs the whole task body inline, including its own
    # DB queries -- those must stay on the same thread/connection as the
    # rest of this request/transaction, not a pooled thread with a separate
    # connection that can't see the just-created, not-yet-committed invite.
    await sync_to_async(send_invite_email_task.delay, thread_sensitive=True)(
        str(invite.id), raw_token, request.auth.get_username(),
    )
    await sync_to_async(log_member_invited, thread_sensitive=True)(company, request.auth, invite.email)
    await invite.arefresh_from_db(fields=['email_sent'])
    message = (
        'Invitation sent successfully' if invite.email_sent
        else 'Invitation created. The email is being sent and will be retried automatically if it fails.'
    )
    return payload(message, 200, True, {'email': invite.email, 'email_sent': invite.email_sent})


@api.post('/emp/accept_invite/', response={201: ApiResponse, 400: ApiResponse, 422: ApiResponse})
@rate_limit('accept_invite', limit=10, window_seconds=600)
async def accept_invite(
    request,
    token: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    profession: str = Form(''),
    phone_number: str = Form(''),
    address: str = Form(''),
    profile_picture: UploadedFile = File(...),
):
    if not 1 <= len(token) <= 64:
        return payload('Invalid or expired invitation token.', 400, False)
    if not 2 <= len(full_name.strip()) <= 301:
        return payload('Enter your full name.', 400, False)
    if len(profession) > 100 or len(phone_number) > 20 or len(address) > 200:
        return payload('One or more profile fields are too long.', 400, False)
    if len(password) > 128:
        return payload('Validation error', 400, False, errors={'password': ['Password must be at most 128 characters.']})
    try:
        await sync_to_async(validate_password, thread_sensitive=True)(password)
    except DjangoValidationError as exc:
        return payload('Validation error', 400, False, errors={'password': exc.messages})
    if profile_picture.size > 5 * 1024 * 1024:
        return payload('Profile picture exceeds the maximum allowed size (5MB).', 400, False)
    if (profile_picture.content_type or '') not in {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}:
        return payload('Profile picture must be a PNG, JPEG, GIF, or WEBP image.', 400, False)
    user, error = await sync_to_async(accept_invite_in_transaction, thread_sensitive=True)(
        token, password, full_name, profession, phone_number, address, profile_picture,
    )
    if error:
        message = (
            'An account with this email already exists.' if error == 'existing_user'
            else 'Invalid or expired invitation token.'
        )
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
            customer = await sync_to_async(stripe.Customer.retrieve, thread_sensitive=False)(
                subscription.stripe_customer_id,
            )
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


def _check_database() -> bool:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        return cursor.fetchone() == (1,)


@api.get('/health/', response={200: ApiResponse, 503: ApiResponse})
async def check_server_health(request):
    """Readiness check (Phase 11): reports unhealthy if the database isn't
    reachable, rather than an unconditional 'running'."""
    try:
        db_ok = await sync_to_async(_check_database, thread_sensitive=True)()
    except Exception:
        logger.exception('health_check.database_unreachable')
        db_ok = False
    if not db_ok:
        return payload('Service unavailable.', 503, False, {'database': 'down'})
    return payload('Service healthy.', 200, True, {'database': 'up'})
