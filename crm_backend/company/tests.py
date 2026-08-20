"""company.services: the role-resolution primitives every authorization
check in the app is built on top of (see that module's own docstring).
Called directly via asgiref.sync.async_to_sync, matching how api.api itself
bridges these async primitives into the sync Django test client elsewhere.
"""

from api.tests import TwoCompanyTestCase
from asgiref.sync import async_to_sync
from users.models import CompanyUserProfile, User

from company.services import get_company_role, get_managed_company


class ManagedCompanyRoleTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.cm_a = User.objects.create_user(email='cm-a@example.com', username='cm-a', password='Kx9#mQ2vLp8Z')
        CompanyUserProfile.objects.create(
            user=self.cm_a, company=self.company_a, role=CompanyUserProfile.Role.COMPANY_MANAGER,
        )

    def test_get_managed_company_returns_the_company_for_a_company_manager(self):
        company = async_to_sync(get_managed_company)(self.cm_a)
        self.assertEqual(company, self.company_a)

    def test_get_managed_company_returns_none_for_a_department_member(self):
        company = async_to_sync(get_managed_company)(self.member_a)
        self.assertIsNone(company)

    def test_get_company_role_returns_cm_for_a_company_manager(self):
        role = async_to_sync(get_company_role)(self.cm_a, self.company_a)
        self.assertEqual(role, CompanyUserProfile.Role.COMPANY_MANAGER)

    def test_get_company_role_is_none_for_a_company_manager_in_the_wrong_company(self):
        role = async_to_sync(get_company_role)(self.cm_a, self.company_b)
        self.assertIsNone(role)
