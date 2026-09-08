"""Resolving which company a caller is acting in, and who they are inside it.

Every resolver in `company.services` answered "which company", which quietly
assumed the answer was unique. It is today, but the assumption was spread
across every call site instead of stated in one place, and the
User/CompanyUserProfile split exists precisely so it would not have to hold
forever.

`resolve_company_context` states it once. The security property worth pinning:
the `company_id` it accepts is a **selector among the caller's own
memberships**, never a grant. Passing another company's id resolves to nothing,
exactly as if it did not exist.
"""

from uuid import uuid4

from api.tests import TwoCompanyTestCase
from asgiref.sync import async_to_sync
from users.models import CompanyUserProfile, User

from company.services import (
    get_member_company,
    resolve_company_context,
    resolve_company_context_sync,
)

PASSWORD = 'Kx9#mQ2vLp8Z'


class CompanyContextTests(TwoCompanyTestCase):
    def resolve(self, user, company_id=None):
        return async_to_sync(resolve_company_context)(user, company_id)

    # -- the caller's own company ------------------------------------------

    def test_an_owner_resolves_to_the_company_they_own(self):
        context = self.resolve(self.owner_a)
        self.assertEqual(context.company.id, self.company_a.id)
        self.assertTrue(context.is_owner)
        self.assertEqual(context.role, CompanyUserProfile.Role.Owner)

    def test_an_owner_carries_their_membership_row(self):
        """Every owner has one since users migration 0008, so the context can
        always hand callers the row rather than just the role."""
        context = self.resolve(self.owner_a)
        self.assertIsNotNone(context.membership)
        self.assertEqual(context.membership.user_id, self.owner_a.id)

    def test_a_member_resolves_to_their_company_and_role(self):
        context = self.resolve(self.member_a)
        self.assertEqual(context.company.id, self.company_a.id)
        self.assertFalse(context.is_owner)
        self.assertEqual(context.role, CompanyUserProfile.Role.DEPARTMENT_MEMBER)

    def test_a_members_department_comes_through(self):
        context = self.resolve(self.member_a)
        self.assertEqual(context.department_id, self.department_a.id)

    def test_an_owner_is_never_department_scoped(self):
        """Matches get_member_department_id, which returns None for the owner
        whatever their profile says."""
        profile = CompanyUserProfile.objects.get(user=self.owner_a, company=self.company_a)
        profile.department = self.department_a
        profile.save(update_fields=['department'])

        self.assertIsNone(self.resolve(self.owner_a).department_id)

    def test_someone_with_no_company_resolves_to_nothing(self):
        nobody = User.objects.create_user(email='nobody@example.com', username='nobody', password=PASSWORD)
        self.assertIsNone(self.resolve(nobody))

    def test_a_deactivated_member_resolves_to_nothing(self):
        """Unchanged behaviour, restated here because it is the mechanism that
        makes deactivation revoke company access without touching Django auth."""
        profile = CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a)
        profile.is_active = False
        profile.save(update_fields=['is_active'])

        self.assertIsNone(self.resolve(self.member_a))

    # -- an explicit company id is a selector, not a grant -----------------

    def test_an_explicit_id_for_your_own_company_works(self):
        context = self.resolve(self.member_a, self.company_a.id)
        self.assertEqual(context.company.id, self.company_a.id)
        self.assertEqual(context.role, CompanyUserProfile.Role.DEPARTMENT_MEMBER)

    def test_another_companys_id_resolves_to_nothing(self):
        """The property this function exists to make unambiguous. A caller
        naming a company they do not belong to gets the same answer as naming
        one that does not exist."""
        self.assertIsNone(self.resolve(self.member_a, self.company_b.id))

    def test_an_owner_cannot_reach_another_company_by_id_either(self):
        self.assertIsNone(self.resolve(self.owner_a, self.company_b.id))

    def test_an_id_for_no_company_at_all_resolves_to_nothing(self):
        """Same answer as naming a company you do not belong to -- an id that
        matches nothing must not be distinguishable from one you may not
        reach."""
        self.assertIsNone(self.resolve(self.member_a, uuid4()))

    def test_a_deactivated_membership_cannot_be_selected_by_id(self):
        profile = CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a)
        profile.is_active = False
        profile.save(update_fields=['is_active'])

        self.assertIsNone(self.resolve(self.member_a, self.company_a.id))

    # -- the fallback is deterministic -------------------------------------

    def test_ownership_wins_over_a_membership_elsewhere(self):
        """Existing behaviour, pinned: get_member_company checked ownership
        first, and anything reading the context must keep doing so."""
        CompanyUserProfile.objects.create(
            user=self.owner_a, company=self.company_b, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        context = self.resolve(self.owner_a)
        self.assertEqual(context.company.id, self.company_a.id)
        self.assertTrue(context.is_owner)

    def test_the_second_membership_is_reachable_by_id(self):
        """What taking an id buys: today the fallback picks one company, and
        the other is unreachable. It no longer has to be."""
        CompanyUserProfile.objects.create(
            user=self.member_a, company=self.company_b, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        context = self.resolve(self.member_a, self.company_b.id)
        self.assertEqual(context.company.id, self.company_b.id)

    def test_the_fallback_is_the_oldest_membership_not_an_arbitrary_row(self):
        """The `.afirst()` this replaces had no ordering, so a user holding two
        memberships got whichever row the database felt like returning."""
        CompanyUserProfile.objects.create(
            user=self.member_a, company=self.company_b, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        for _ in range(3):
            self.assertEqual(self.resolve(self.member_a).company.id, self.company_a.id)

    # -- behaviour preservation --------------------------------------------

    def test_get_member_company_still_answers_exactly_as_before(self):
        """It delegates now, so there is one implementation of the fallback
        rather than two that can drift apart."""
        for user, expected in (
            (self.owner_a, self.company_a.id),
            (self.member_a, self.company_a.id),
            (self.owner_b, self.company_b.id),
        ):
            self.assertEqual(async_to_sync(get_member_company)(user).id, expected)

    def test_the_sync_mirror_agrees_with_the_async_one(self):
        """Two implementations exist only because Django transactions are
        sync-only. They must not disagree."""
        for user in (self.owner_a, self.member_a, self.owner_b):
            async_context = self.resolve(user)
            sync_context = resolve_company_context_sync(user)
            self.assertEqual(async_context.company.id, sync_context.company.id)
            self.assertEqual(async_context.role, sync_context.role)
            self.assertEqual(async_context.is_owner, sync_context.is_owner)

    def test_the_sync_mirror_refuses_another_company_too(self):
        self.assertIsNone(resolve_company_context_sync(self.member_a, self.company_b.id))


class CompanyContextDepartmentTests(TwoCompanyTestCase):
    """department_id is the one derived field on the context, and it has to
    match the resolver it replaces."""

    def test_a_member_with_no_department_resolves_to_none(self):
        user = User.objects.create_user(email='nodept@example.com', username='nodept', password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )
        self.assertIsNone(async_to_sync(resolve_company_context)(user).department_id)

    def test_a_members_department_is_the_one_on_their_membership(self):
        context = async_to_sync(resolve_company_context)(self.member_a)
        self.assertEqual(context.department_id, context.membership.department_id)
