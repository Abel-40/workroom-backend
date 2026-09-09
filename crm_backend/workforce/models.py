"""Who the people in a company are, and how much they can take on.

Three things live here, and they are related in one direction only:

**Catalogs.** ``Profession`` and ``Skill`` are company-scoped controlled
vocabularies, seeded from sector templates the same way departments, task
types and event types already are. Nothing anywhere accepts a free-text skill.
That is not tidiness: matching data fragments the moment it can, and
"Python" / "python" / "Python 3" as three separate strings makes every
question you would ask of it -- who can do this, who is under-used -- quietly
wrong rather than loudly broken.

**Claims.** ``MemberSkill`` attaches a skill to a *membership*, not to a user,
at a stated level. Somebody may be an expert in one company and learning in
another, and a person who leaves a company should not carry its skill
taxonomy with them.

**Limits.** ``AssignmentPolicy`` is one row per company describing when
assigning more work should warn somebody or stop them. It never assigns
anything itself: there is no load balancer, no auto-assignment and no scoring
engine here, deliberately. It answers one question at one moment -- "is this
particular assignment over the line" -- and leaves the decision to a person.

Capacity itself (``weekly_capacity_hours``, ``availability``) lives on
``users.CompanyUserProfile`` rather than in this module, for the same reason
``MemberSkill`` points at the membership: how many hours a week somebody works
is a fact about a job, not about a person.
"""

from django.db import models
from django.db.models.functions import Lower
from utils.models import UUIDModel


class DefaultProfession(UUIDModel):
    """Global template row, mirroring ``DefaultDepartment`` and
    ``DefaultEventType``. ``sector=None`` means "offered to every company"."""

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    sector = models.ForeignKey(
        'company.Sector', on_delete=models.CASCADE, related_name='default_professions',
        null=True, blank=True,
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.sector.name if self.sector else "All Sectors"})'


class Profession(UUIDModel):
    """What somebody does, drawn from a company-scoped list rather than typed.

    Replaces the free-text ``CompanyUserProfile.profession`` string, which
    defaulted to the literal ``'Not provided'`` and was therefore three
    different things at once: a real answer, a placeholder, and a null.
    """

    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='professions')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    default_profession = models.ForeignKey(
        DefaultProfession, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='company_professions',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        constraints = [
            # Case-insensitive, unlike the plain unique_together the older
            # catalogs use. "Backend Engineer" and "backend engineer" are one
            # profession, and letting both exist is the first step towards the
            # fragmentation this module exists to prevent.
            models.UniqueConstraint(
                'company', Lower('name'), name='one_profession_per_company_name',
            ),
        ]

    def __str__(self):
        return f'{self.name} ({self.company_id})'


class DefaultSkill(UUIDModel):
    """Template row for the seeded skill catalog.

    ``category`` is a plain string here and on ``Skill``. It groups skills for
    display and nothing else -- no permission, no matching rule and no AI
    context reads it -- so a fourth template table to control it would be
    machinery in exchange for nothing. Company-created skills reuse an
    existing category's exact spelling where one matches case-insensitively;
    see :func:`workforce.services.normalize_category`.
    """

    name = models.CharField(max_length=100)
    category = models.CharField(max_length=60)
    description = models.TextField(blank=True, default='')
    sector = models.ForeignKey(
        'company.Sector', on_delete=models.CASCADE, related_name='default_skills',
        null=True, blank=True,
    )

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return f'{self.name} [{self.category}]'


class Skill(UUIDModel):
    """One skill a company recognises. There is no other kind."""

    company = models.ForeignKey('company.Company', on_delete=models.CASCADE, related_name='skills')
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=60, blank=True, default='')
    description = models.TextField(blank=True, default='')
    default_skill = models.ForeignKey(
        DefaultSkill, on_delete=models.SET_NULL, null=True, blank=True, related_name='company_skills',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category', 'name']
        constraints = [
            models.UniqueConstraint('company', Lower('name'), name='one_skill_per_company_name'),
        ]
        indexes = [
            models.Index(fields=['company', 'category']),
        ]

    def __str__(self):
        return f'{self.name} ({self.company_id})'


class MemberSkill(UUIDModel):
    """One person's claim to one skill, at a level, within one company."""

    class Level(models.TextChoices):
        LEARNING = 'learning', 'Learning'
        WORKING = 'working', 'Working knowledge'
        STRONG = 'strong', 'Strong'
        EXPERT = 'expert', 'Expert'

    # The membership, not the user: see this module's docstring.
    profile = models.ForeignKey(
        'users.CompanyUserProfile', on_delete=models.CASCADE, related_name='member_skills',
    )
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name='member_skills')
    level = models.CharField(max_length=20, choices=Level.choices, default=Level.WORKING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['skill__category', 'skill__name']
        constraints = [
            # Two levels for the same skill would make "how good are they"
            # ambiguous at exactly the moment somebody asks.
            models.UniqueConstraint(fields=['profile', 'skill'], name='one_level_per_profile_skill'),
        ]
        indexes = [
            models.Index(fields=['skill', 'level']),
        ]

    def __str__(self):
        return f'{self.profile_id}: {self.skill_id} ({self.level})'


class AssignmentPolicy(UUIDModel):
    """When giving somebody more work should say something, or stop.

    One row per company, created on demand and absent by default -- an absent
    policy means no policy, which is the correct behaviour for every company
    that has never opened the setting.

    The two limits answer different questions and are deliberately not merged:

    ``max_active_tasks``
        The cheap, always-available guardrail. Counting tasks needs no
        estimates and no stated capacity, so it works for everybody on day
        one.
    ``max_utilisation_pct``
        Committed hours against prorated capacity. More meaningful and much
        more conditional: it can only fire for somebody who has stated a
        weekly capacity, and it is only honest when the tasks involved carry
        estimates. Where either is missing the limit does not fire -- see
        :func:`workforce.services.evaluate_assignment_sync`, which would
        rather say nothing than say something false.

    ``enforcement`` defaults to ``warn`` because the first useful thing a
    limit does is tell somebody a number they did not have. Blocking is a
    second decision, and a company that has never looked at the numbers is not
    ready to make it.
    """

    class Enforcement(models.TextChoices):
        WARN = 'warn', 'Warn'
        BLOCK = 'block', 'Block'

    company = models.OneToOneField('company.Company', on_delete=models.CASCADE, related_name='assignment_policy')
    enabled = models.BooleanField(default=False)
    max_active_tasks = models.PositiveSmallIntegerField(null=True, blank=True)
    max_utilisation_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    enforcement = models.CharField(max_length=10, choices=Enforcement.choices, default=Enforcement.WARN)
    # Role codes (CompanyUserProfile.Role values) permitted to override a
    # block with a stated reason. An assigner outside this list does not get a
    # refusal they can argue with -- they get an ApprovalRequest routed to
    # somebody who can decide.
    override_roles = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.company_id} ({self.enforcement}, enabled={self.enabled})'
