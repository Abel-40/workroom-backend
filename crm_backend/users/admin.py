from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, CompanyUserProfile

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('id','email', 'username', 'is_staff', 'is_active', 'date_joined')
    list_filter = ('is_staff', 'is_active', 'date_joined')
    search_fields = ('email', 'username')
    ordering = ('email',)
    fieldsets = (
        (None, {'fields': ('email', 'username', 'password')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'username', 'password1', 'password2'),
        }),
    )

@admin.register(CompanyUserProfile)
class CompanyUserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'company', 'role', 'department', 'profession', 'created_at')
    list_filter = ('role', 'department', 'created_at')
    search_fields = ('user__email', 'user__username', 'profession', 'address', 'phone_number')
    raw_id_fields = ('user', 'company', 'department')
