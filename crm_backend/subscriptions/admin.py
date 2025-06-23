from django.contrib import admin
from .models import Subscription

@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('company', 'plan', 'status', 'is_trial', 'start_date', 'current_period_end', 'created_at')
    list_filter = ('status', 'is_trial', 'start_date', 'created_at')
    search_fields = ('company__name', 'stripe_customer_id', 'stripe_subscription_id')
    date_hierarchy = 'start_date'
    raw_id_fields = ('company', 'plan')
