"""Company settings, and the one setting there is.

`allow_public_projects` is the gate WP6 put in front of `public` visibility.
A gate nobody can open is not a gate, it is a wall, so these tests care as much
about the Owner being able to switch it on as about everyone else being unable
to.
"""

import json
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from audit.models import AuditAction, AuditEvent
from django.utils import timezone
from projects_and_tasks.models import Project
from users.models import CompanyUserProfile, User

PASSWORD = 'Kx9#mQ2vLp8Z'


class CompanySettingsTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.cm = User.objects.create_user(email='cm@example.com', username='cm', password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=self.cm, company=self.company_a, department=self.department_a,
            role=CompanyUserProfile.Role.COMPANY_MANAGER,
        )

    def get_settings(self, actor):
        return self.client.get('/api/v1/company/settings/', **auth_header(actor))

    def patch_settings(self, actor, **fields):
        return self.client.patch(
            '/api/v1/company/settings/', json.dumps(fields),
            content_type='application/json', **auth_header(actor),
        )

    def settings_events(self):
        return list(AuditEvent.objects.filter(action=AuditAction.COMPANY_SETTINGS_CHANGED).order_by('created_at'))

    # -- reading -----------------------------------------------------------

    def test_any_member_can_read_the_settings(self):
        """The project form has to know whether `public` is on the menu before
        it offers it."""
        response = self.get_settings(self.member_a)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIs(response.json()['data']['settings']['allow_public_projects'], False)

    def test_someone_with_no_company_gets_404(self):
        stranger = User.objects.create_user(email='nobody@example.com', username='nobody', password=PASSWORD)
        self.assertEqual(self.get_settings(stranger).status_code, 404)

    # -- writing -----------------------------------------------------------

    def test_the_owner_can_switch_public_projects_on(self):
        response = self.patch_settings(self.owner_a, allow_public_projects=True)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIs(response.json()['data']['settings']['allow_public_projects'], True)
        self.company_a.refresh_from_db()
        self.assertIs(self.company_a.allow_public_projects, True)

    def test_a_company_manager_cannot(self):
        """Narrower than every other admin endpoint, deliberately: this decides
        whether the company's work can leave the company."""
        self.assertEqual(self.patch_settings(self.cm, allow_public_projects=True).status_code, 403)
        self.company_a.refresh_from_db()
        self.assertIs(self.company_a.allow_public_projects, False)

    def test_a_department_member_cannot(self):
        self.assertEqual(self.patch_settings(self.member_a, allow_public_projects=True).status_code, 403)
        self.company_a.refresh_from_db()
        self.assertIs(self.company_a.allow_public_projects, False)

    def test_another_companys_owner_changes_only_their_own_company(self):
        """The company is derived from the caller, never supplied, so there is
        no id to tamper with -- owner B's write lands on company B."""
        self.assertEqual(self.patch_settings(self.owner_b, allow_public_projects=True).status_code, 200)
        self.company_a.refresh_from_db()
        self.company_b.refresh_from_db()
        self.assertIs(self.company_a.allow_public_projects, False)
        self.assertIs(self.company_b.allow_public_projects, True)

    def test_an_explicit_null_is_treated_as_not_set(self):
        """The field is optional so that a PATCH naming one setting does not
        reset the others; `null` is that same absence, not a value to write
        into a non-nullable column."""
        self.company_a.allow_public_projects = True
        self.company_a.save(update_fields=['allow_public_projects'])
        response = self.client.patch(
            '/api/v1/company/settings/', json.dumps({'allow_public_projects': None}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.company_a.refresh_from_db()
        self.assertIs(self.company_a.allow_public_projects, True)

    def test_a_patch_that_names_nothing_leaves_the_setting_alone(self):
        self.company_a.allow_public_projects = True
        self.company_a.save(update_fields=['allow_public_projects'])
        self.assertEqual(self.patch_settings(self.owner_a).status_code, 200)
        self.company_a.refresh_from_db()
        self.assertIs(self.company_a.allow_public_projects, True)

    # -- audit -------------------------------------------------------------

    def test_the_change_is_audited_with_both_sides(self):
        self.assertEqual(self.settings_events(), [])
        self.patch_settings(self.owner_a, allow_public_projects=True)
        events = self.settings_events()
        self.assertEqual(len(events), 1)
        self.assertIs(events[0].before['allow_public_projects'], False)
        self.assertIs(events[0].after['allow_public_projects'], True)
        self.assertEqual(events[0].actor_id, self.owner_a.id)

    def test_a_no_op_write_records_nothing(self):
        """Writing the value it already has is not a change, and a row saying
        so would be noise in the one log that has to stay readable."""
        self.patch_settings(self.owner_a, allow_public_projects=False)
        self.assertEqual(self.settings_events(), [])

    # -- the point of the whole thing --------------------------------------

    def test_switching_it_on_is_what_makes_a_project_publishable(self):
        """End to end: the gate refuses, the Owner opens it, the same call now
        succeeds. Without this the setting is read by the gate and written by
        nobody, and `public` is unreachable for every company."""
        now = timezone.now()
        project = Project.objects.create(
            title='Website Revamp', company=self.company_a, department=self.department_a,
            visibility=Project.VISIBILITY.PRIVATE, start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )

        def publish():
            return self.client.patch(
                f'/api/v1/projects/{project.id}/', json.dumps({'visibility': Project.VISIBILITY.PUBLIC}),
                content_type='application/json', **auth_header(self.owner_a),
            )

        self.assertEqual(publish().status_code, 403)
        self.assertEqual(self.patch_settings(self.owner_a, allow_public_projects=True).status_code, 200)
        self.assertEqual(publish().status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.visibility, Project.VISIBILITY.PUBLIC)
