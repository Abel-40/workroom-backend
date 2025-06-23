from django.contrib import admin
from .models import Company, Sector

@admin.register(Sector)
class SectorAdmin(admin.ModelAdmin):
    list_display = ('id','name',)
    search_fields = ('name', 'description')

@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('id','name', 'code', 'sector', 'owner', 'subscription_status', 'is_trial', 'created_at')
    list_filter = ('sector', 'subscription_status', 'is_trial', 'created_at')
    search_fields = ('name', 'code', 'stripe_customer_id', 'stripe_subscription_id')
    date_hierarchy = 'created_at'
    raw_id_fields = ('owner', 'plan')
