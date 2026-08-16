from company.models import Sector
from django.core.management.base import BaseCommand

from departments_and_teams.models import DefaultDepartment


class Command(BaseCommand):
    help = "Seed default departments offered during company onboarding"

    # sector=None means "All Sectors" -- offered regardless of the company's sector.
    GLOBAL_DEPARTMENTS = [
        {"name": "Human Resources", "description": "Hiring, onboarding, and employee management."},
        {"name": "Finance & Accounting", "description": "Budgeting, accounting, and financial reporting."},
        {"name": "Operations", "description": "Day-to-day business operations."},
        {"name": "Sales", "description": "Revenue generation and client acquisition."},
        {"name": "Customer Support", "description": "Customer service and support."},
        {"name": "IT & Infrastructure", "description": "Internal tooling and technical infrastructure."},
        {"name": "Legal & Compliance", "description": "Legal affairs and regulatory compliance."},
    ]

    SECTOR_DEPARTMENTS = {
        "Software & Technology": [
            {"name": "Engineering", "description": "Product and platform engineering."},
            {"name": "Product", "description": "Product management and strategy."},
            {"name": "Quality Assurance", "description": "Testing and quality assurance."},
            {"name": "DevOps", "description": "Infrastructure, deployment, and reliability."},
        ],
        "Marketing & Advertising": [
            {"name": "Content", "description": "Content creation and copywriting."},
            {"name": "Brand & Creative", "description": "Brand strategy and creative design."},
            {"name": "Growth Marketing", "description": "Performance marketing and growth campaigns."},
        ],
        "Healthcare": [
            {"name": "Clinical Operations", "description": "Patient care and clinical workflows."},
        ],
        "Retail & E-commerce": [
            {"name": "Merchandising", "description": "Product selection and inventory strategy."},
            {"name": "Logistics & Supply Chain", "description": "Fulfillment and supply chain management."},
        ],
        "Education": [
            {"name": "Curriculum & Instruction", "description": "Course design and instruction."},
            {"name": "Admissions", "description": "Student recruitment and admissions."},
        ],
    }

    def handle(self, *args, **options):
        for data in self.GLOBAL_DEPARTMENTS:
            self._create(data, sector=None)

        for sector_name, departments in self.SECTOR_DEPARTMENTS.items():
            sector = Sector.objects.filter(name=sector_name).first()
            if sector is None:
                self.stdout.write(self.style.WARNING(
                    f"Skipping '{sector_name}' departments: sector not found. Run seed_sectors first.",
                ))
                continue
            for data in departments:
                self._create(data, sector=sector)

    def _create(self, data, sector):
        department, created = DefaultDepartment.objects.get_or_create(
            name=data["name"], sector=sector, defaults={"description": data["description"]},
        )
        label = sector.name if sector else "All Sectors"
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created department: {department.name} ({label})"))
        else:
            self.stdout.write(self.style.WARNING(f"Already exists: {department.name} ({label})"))
