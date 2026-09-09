"""Seed the profession and skill templates offered during onboarding.

Same shape as ``seed_default_departments`` and ``seed_default_task_types``:
sector=None rows are offered to every company, sector-keyed rows only to
companies in that sector. Idempotent -- re-running updates descriptions and
adds anything new without duplicating.

The catalog is deliberately short. A seeded list is a starting point somebody
edits, not an attempt at a taxonomy of all work: two hundred rows would make
the picker useless and every company would still add their own.
"""

from company.models import Sector
from django.core.management.base import BaseCommand

from workforce.models import DefaultProfession, DefaultSkill


class Command(BaseCommand):
    help = 'Seed default professions and skills offered during company onboarding'

    GLOBAL_PROFESSIONS = [
        ('Project Manager', 'Plans, coordinates and tracks delivery.'),
        ('Business Analyst', 'Gathers requirements and defines process.'),
        ('Accountant', 'Bookkeeping, reporting and reconciliation.'),
        ('Human Resources Specialist', 'Hiring, onboarding and employee relations.'),
        ('Sales Representative', 'Prospecting, pitching and closing.'),
        ('Customer Support Specialist', 'Resolves customer issues and questions.'),
        ('Operations Coordinator', 'Keeps day-to-day operations running.'),
        ('Marketing Specialist', 'Campaigns, content and channels.'),
    ]

    SECTOR_PROFESSIONS = {
        'Software & Technology': [
            ('Backend Engineer', 'Server-side systems, APIs and data.'),
            ('Frontend Engineer', 'User interfaces and client applications.'),
            ('Full Stack Engineer', 'Both ends of the application.'),
            ('QA Engineer', 'Test design, automation and release quality.'),
            ('DevOps Engineer', 'Infrastructure, deployment and reliability.'),
            ('Product Designer', 'Interaction and interface design.'),
            ('Data Analyst', 'Reporting, dashboards and analysis.'),
        ],
        'Marketing & Advertising': [
            ('Content Writer', 'Long- and short-form copy.'),
            ('Graphic Designer', 'Visual design and brand assets.'),
            ('SEO Specialist', 'Search visibility and content strategy.'),
        ],
    }

    GLOBAL_SKILLS = [
        ('Project Planning', 'Delivery', 'Scoping, sequencing and scheduling work.'),
        ('Stakeholder Communication', 'Delivery', 'Keeping the right people informed.'),
        ('Requirements Analysis', 'Delivery', 'Turning needs into specifications.'),
        ('Budgeting', 'Finance', 'Planning and tracking spend.'),
        ('Financial Reporting', 'Finance', 'Statements, forecasts and reconciliation.'),
        ('Recruiting', 'People', 'Sourcing, interviewing and hiring.'),
        ('Onboarding', 'People', 'Bringing new members up to speed.'),
        ('Negotiation', 'Commercial', 'Reaching workable agreements.'),
        ('Customer Support', 'Commercial', 'Resolving customer problems.'),
        ('Copywriting', 'Content', 'Writing for a purpose and an audience.'),
        ('Data Analysis', 'Data', 'Drawing conclusions from figures.'),
        ('Spreadsheet Modelling', 'Data', 'Building and auditing models.'),
    ]

    SECTOR_SKILLS = {
        'Software & Technology': [
            ('Python', 'Engineering', 'Backend and scripting.'),
            ('JavaScript', 'Engineering', 'Browser and Node applications.'),
            ('TypeScript', 'Engineering', 'Typed JavaScript.'),
            ('Django', 'Engineering', 'Python web framework.'),
            ('React', 'Engineering', 'Component-based UI library.'),
            ('Vue', 'Engineering', 'Component-based UI framework.'),
            ('SQL', 'Engineering', 'Relational querying and schema design.'),
            ('PostgreSQL', 'Engineering', 'Relational database administration.'),
            ('Docker', 'Infrastructure', 'Containerisation and local environments.'),
            ('CI/CD', 'Infrastructure', 'Automated build, test and release.'),
            ('Cloud Infrastructure', 'Infrastructure', 'Provisioning and operating hosted systems.'),
            ('Automated Testing', 'Quality', 'Unit, integration and end-to-end tests.'),
            ('Code Review', 'Quality', 'Reading code for correctness and clarity.'),
            ('API Design', 'Engineering', 'Contracts, versioning and error shapes.'),
            ('UI Design', 'Design', 'Layout, hierarchy and visual detail.'),
            ('UX Research', 'Design', 'Understanding how people use a product.'),
        ],
        'Marketing & Advertising': [
            ('SEO', 'Content', 'Search visibility and keyword strategy.'),
            ('Brand Strategy', 'Content', 'Positioning and identity.'),
            ('Social Media', 'Content', 'Channel planning and community.'),
            ('Adobe Creative Suite', 'Design', 'Industry-standard design tooling.'),
        ],
    }

    def handle(self, *args, **options):
        professions = self._seed_professions()
        skills = self._seed_skills()
        self.stdout.write(self.style.SUCCESS(
            f'Seeded {professions} professions and {skills} skills.'
        ))

    def _seed_professions(self) -> int:
        count = 0
        for name, description in self.GLOBAL_PROFESSIONS:
            DefaultProfession.objects.update_or_create(
                name=name, sector=None, defaults={'description': description},
            )
            count += 1
        for sector_name, entries in self.SECTOR_PROFESSIONS.items():
            sector = Sector.objects.filter(name=sector_name).first()
            if sector is None:
                self.stdout.write(self.style.WARNING(
                    f'Sector "{sector_name}" not found -- run seed_sectors first. Skipping its professions.'
                ))
                continue
            for name, description in entries:
                DefaultProfession.objects.update_or_create(
                    name=name, sector=sector, defaults={'description': description},
                )
                count += 1
        return count

    def _seed_skills(self) -> int:
        count = 0
        for name, category, description in self.GLOBAL_SKILLS:
            DefaultSkill.objects.update_or_create(
                name=name, sector=None, defaults={'category': category, 'description': description},
            )
            count += 1
        for sector_name, entries in self.SECTOR_SKILLS.items():
            sector = Sector.objects.filter(name=sector_name).first()
            if sector is None:
                self.stdout.write(self.style.WARNING(
                    f'Sector "{sector_name}" not found -- run seed_sectors first. Skipping its skills.'
                ))
                continue
            for name, category, description in entries:
                DefaultSkill.objects.update_or_create(
                    name=name, sector=sector, defaults={'category': category, 'description': description},
                )
                count += 1
        return count
