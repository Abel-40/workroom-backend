# subscriptions/views.py
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from ..serializers.subscription_serializers import SubscriptionSerializer,Subscription
from company.models import Company
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from django.conf import settings
from plans.models import Plan
from utils.api_response import api_response
import stripe
stripe.api_key = settings.STRIPE_SECRET_KEY
from utils.api_response import api_response
from rest_framework.permissions import AllowAny
class SubscriptionViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=["post"], url_path="start-checkout")
    def start_checkout(self, request):
        plan_id = request.data.get("plan_id")
        user = request.user
        
        try:
            plan = Plan.objects.get(id=plan_id)
            company = Company.objects.get(owner=user)
        except (Plan.DoesNotExist, Company.DoesNotExist, AttributeError):
            return api_response(
                message="Invalid plan or company.",
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                errors={"plan_id": ["Invalid plan or company"]}
            )

        
        subscription = getattr(company, "subscription", None)
        if not subscription:
            subscription = Subscription.objects.create(company=company, plan=plan)

        
        if not subscription.stripe_customer_id:
            customer = stripe.Customer.create(
                email=user.email,
                name=company.name,
                metadata={"company_id": company.id}
            )
            subscription.stripe_customer_id = customer.id
            subscription.save()
        else:
            customer = stripe.Customer.retrieve(subscription.stripe_customer_id)

        
        try:
            session = stripe.checkout.Session.create(
                customer=customer.id,
                payment_method_types=['card'],
                line_items=[{
                    'price': plan.stripe_price_id,
                    'quantity': 1,
                }],
                mode='subscription',
                success_url='http://localhost:3000/success?session_id={CHECKOUT_SESSION_ID}',
                cancel_url='http://localhost:3000/cancel',
            )
        except Exception as e:
            return api_response(
                message="Stripe session creation failed.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                success=False,
                errors={"stripe": [str(e)]}
            )
            
        return api_response(
            message="Checkout session created successfully.",
            status_code=status.HTTP_200_OK,
            success=True,
            data={"checkout_url": session.url}
        )

    @action(detail=False, methods=["get"], url_path="my-subscription")
    def get_my_subscription(self, request):
        user = request.user
        company = Company.objects.get(owner=user)
        try:
            subscription = company.subscription  
        except AttributeError:
            return api_response(
                message="No subscription found.",
                status_code=status.HTTP_404_NOT_FOUND,
                success=False,
                errors={"subscription": ["No active subscription found for your company."]}
            )

        serializer = SubscriptionSerializer(subscription)
        return api_response(
            message="Subscription retrieved successfully.",
            status_code=status.HTTP_200_OK,
            success=True,
            data=serializer.data
        )



