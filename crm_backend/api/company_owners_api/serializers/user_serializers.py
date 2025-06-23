from  users.models import User,CompanyUserProfile,PendingInvite
from rest_framework import serializers
from django.contrib.auth import get_user_model

User = get_user_model()
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'username',
            'first_name',
            'last_name',
            'password',
        ]
        read_only_fields = ('id',)
        extra_kwargs = {'password':{'write_only':True}}
    def create(self, validated_data):
        user = User.objects.create_user(**validated_data)
        return user
    def validate(self, attrs):
      if not attrs.get('password'):
          raise serializers.ValidationError("Please enter a password")
      return attrs
    def validate_email(self, value):
      if User.objects.filter(email=value).exists():
          raise serializers.ValidationError("This email is already registered.")
      return value
  
  
class PendingUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = PendingInvite
        fields = ('id','email','token','company','department','role','status','created_at','expires_at')
        read_only_fields = ('token','status','created_at','expires_at')