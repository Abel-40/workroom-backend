from rest_framework import serializers
from subscriptions.models import Subscription

class SubscriptionSerializer(serializers.ModelSerializer):
    is_active = serializers.SerializerMethodField()
    on_trial = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            'id', 'company', 'plan', 'status', 'is_trial', 'start_date',
            'current_period_end', 'canceled_at',
            'stripe_customer_id', 'stripe_subscription_id',
            'created_at', 'updated_at',
            'is_active', 'on_trial'
        ]
        read_only_fields = ['created_at', 'updated_at', 'is_active', 'on_trial']

    def get_is_active(self, obj):
        return obj.is_active()

    def get_on_trial(self, obj):
        return obj.on_trial()

  
  