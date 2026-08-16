from django.db import models
from django.contrib.auth.models import BaseUserManager,AbstractUser
from django.conf import settings
import uuid
from django.utils import timezone
from datetime import timedelta
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
        DEPARTMENT_LEADER = 'DL', 'Department Leader'
        DEPARTMENT_MEMBER = 'DM', 'Department Member'

    role = models.CharField(max_length=200, choices=Role.choices, default=Role.Owner)
    department = models.ForeignKey('departments_and_teams.Department', on_delete=models.SET_NULL, null=True, blank=True)

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
        DEPARTMENT_LEADER = 'DL', 'Department Leader'
        DEPARTMENT_MEMBER = 'DM', 'Department Member'
        
    class Status(models.TextChoices):
        Pending = 'Pending','Pending'
        Accepted = 'Accepted','Accepted'
        Expired = 'Expired','Expired'
    email = models.EmailField()
    token = models.CharField(max_length=64, unique=True, default=uuid.uuid4)
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE)
    department = models.ForeignKey('departments_and_teams.Department', on_delete=models.SET_NULL, null=True)
    role = models.CharField(max_length=200, choices=Role.choices, default=Role.Owner)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.Pending)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=default_expiration)  

    def is_expired(self):
        return timezone.now() >= self.expires_at

    def __str__(self):
        return f"{self.email} - {self.company.name} - {self.status}"
