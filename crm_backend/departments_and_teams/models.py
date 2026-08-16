from django.db import models
from django.conf import settings
from utils.models import UUIDModel

class Department(UUIDModel):
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='departments')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='leading_departments'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'name')

    def __str__(self):
        return f"{self.name} ({self.company.name})"

class DefaultDepartment(UUIDModel):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    sector = models.ForeignKey(
        'company.Sector',
        on_delete=models.CASCADE,
        related_name='default_departments',
        null=True,  # ✅ null means "global"
        blank=True
    )

    def __str__(self):
        return f"{self.name} ({self.sector.name if self.sector else 'All Sectors'})"

class Team(UUIDModel):
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='teams')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='leading_teams'
    )

    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='team_memberships',
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'name')

    def __str__(self):
        return f"{self.name} ({self.company.name})"
