import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone
from utils.models import UUIDModel


class UserManger(BaseUserManager):
  def create_user(self,email,password,username):
    if email is None:
      raise ValueError('Email required')
    
    email = self.normalize_email(email)
    user = self.model(email=email,username=username)
    user.set_password(password)
    user.is_active = True
    user.save(using=self._db)
    return user
  def create_superuser(self,email,password,username):
    user = self.create_user(email=email,password=password,username=username)
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.save(using= self._db)
    return user
  
class User(AbstractUser):
  id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
  email = models.EmailField(unique=True,max_length=200)
  username = models.CharField(max_length=100,unique=False)
  # IANA timezone name (e.g. "America/New_York"), validated against
  # zoneinfo.available_timezones() in users.services.update_user_timezone --
  # never validated against a DB-driven choices list, since Python's stdlib
  # tzdata is already the authoritative source. Lives on User (not
  # CompanyUserProfile, unlike phone_number/address/etc.) because it's a
  # personal preference, not a company-membership one -- and unlike
  # CompanyUserProfile, every authenticated user unconditionally has a User
  # row, including a company Owner (see users.services
  # .update_notification_preference's 'no_profile' case for the gap this
  # avoids).
  timezone = models.CharField(max_length=64, default='UTC')
  USERNAME_FIELD = "email"
  REQUIRED_FIELDS = ['username']
  objects= UserManger()
  def __str__(self):
    return self.username
  

class CompanyUserProfile(UUIDModel):
      
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='company_profiles')
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='company_user_profiles')
    class Role(models.TextChoices):
        Owner = 'Owner', 'Owner'
        COMPANY_MANAGER = 'CM', 'Company Manager'
        DEPARTMENT_LEADER = 'DL', 'Department Leader'
        DEPARTMENT_MEMBER = 'DM', 'Department Member'

    role = models.CharField(max_length=200, choices=Role.choices, default=Role.Owner)
    department = models.ForeignKey('departments_and_teams.Department', on_delete=models.SET_NULL, null=True, blank=True)
    # Company-scoped lockout, distinct from the global auth User.is_active --
    # see users.services.set_member_active_status and company.services (the
    # tenant-resolution helpers filter on this) for why: deactivating access
    # to one company must never affect Django auth or any other company the
    # same person might belong to.
    is_active = models.BooleanField(default=True)
    # Optional notifications respect this; critical ones always email
    # regardless -- see notifications_and_activity.services.TYPE_CATEGORY.
    email_notifications_enabled = models.BooleanField(default=True)

    # Profile details
    profile_picture = models.ImageField(upload_to='profile_pictures/', null=True, blank=True)
    address = models.CharField(max_length=200, default='Not provided')
    phone_number = models.CharField(max_length=20, default='Not provided')
    resume = models.FileField(upload_to='user_resume/', blank=True, null=True)
    profession = models.CharField(max_length=100,default='Not provided')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'user') 

    def __str__(self):
        return f"{self.user.username} ({self.company.name})"

def default_expiration():
    return timezone.now() + timedelta(days=2)

class PendingInvite(UUIDModel):
    class Role(models.TextChoices):
        Owner = 'Owner', 'Owner'
        COMPANY_MANAGER = 'CM', 'Company Manager'
        DEPARTMENT_LEADER = 'DL', 'Department Leader'
        DEPARTMENT_MEMBER = 'DM', 'Department Member'

    class Status(models.TextChoices):
        # No Accepted state: an accepted invite is deleted outright (see
        # api.accept_invite_in_transaction), not marked and kept around.
        Pending = 'Pending', 'Pending'
        Expired = 'Expired', 'Expired'

    email = models.EmailField()
    # Only the hash is stored; the raw token is emailed to the invitee and
    # never persisted (see utils/tokens.py).
    token_hash = models.CharField(max_length=64, unique=True)
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE)
    department = models.ForeignKey('departments_and_teams.Department', on_delete=models.SET_NULL, null=True)
    role = models.CharField(max_length=200, choices=Role.choices, default=Role.Owner)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.Pending)
    email_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=default_expiration)

    def is_expired(self):
        return timezone.now() >= self.expires_at

    def __str__(self):
        return f"{self.email} - {self.company.name} - {self.status}"
