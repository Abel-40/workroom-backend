"""Who may move a project's visibility, and what gets recorded when they do.

`public` is the only visibility that puts a project outside the tenant
boundary, and before this it was reachable by anyone who could manage the
project -- including a Department Member managing their own. There was no
company-level control and no record that it had happened.

The rules now:

    private <-> department   anyone who can manage the project
    -> company               Department Leader (own department), CM, Owner
    -> public                CM or Owner, and only when the company allows it

Every change is audited.
"""

import json
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from audit.models import AuditAction, AuditEvent
from django.utils import timezone
from users.models import CompanyUserProfile, User

from projects_and_tasks import services
from projects_and_tasks.models import Project

PASSWORD = 'Kx9#mQ2vLp8Z'


class VisibilityWorldMixin:
    def setUp(self):
        super().setUp()
        self.cm = self._member('cm', CompanyUserProfile.Role.COMPANY_MANAGER)
        self.dl = self._member('dl', CompanyUserProfile.Role.DEPARTMENT_LEADER, self.department_a)
        now = timezone.now()
        self.project = Project.objects.create(
            title='Website Revamp', company=self.company_a, department=self.department_a,
            visibility=Project.VISIBILITY.PRIVATE, start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )

    def _member(self, name, role, department=None):
        user = User.objects.create_user(email=f'{name}@example.com', username=name, password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, department=department, role=role,
        )
        return user

    def set_visibility(self, actor, visibility, project=None):
        project = project or self.project
        return self.client.patch(
            f'/api/v1/projects/{project.id}/', json.dumps({'visibility': visibility}),
            content_type='application/json', **auth_header(actor),
        )

    def allow_public(self):
        self.company_a.allow_public_projects = True
        self.company_a.save(update_fields=['allow_public_projects'])

    def visibility_events(self):
        """Audit rows are append-only, so tests count what exists rather than
        clearing the table between assertions -- the model refusing a delete is
        the model working."""
        return list(
            AuditEvent.objects.filter(action=AuditAction.PROJECT_VISIBILITY_CHANGED).order_by('created_at')
        )


class VisibilityGateTests(VisibilityWorldMixin, TwoCompanyTestCase):
    """Who may set which visibility."""

    # -- the public gate ---------------------------------------------------

    def test_public_is_refused_while_the_company_flag_is_off(self):
        """Off by default. Publishing is a decision about the company's own
        exposure, so it is made once by someone who owns that risk rather than
        implicitly by whoever happens to manage a project."""
        for actor in (self.owner_a, self.cm):
            response = self.set_visibility(actor, Project.VISIBILITY.PUBLIC)
            self.assertEqual(response.status_code, 403, f'{actor.email} published with the flag off')
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY.PRIVATE)

    def test_owner_and_company_manager_can_publish_once_it_is_allowed(self):
        self.allow_public()
        for actor in (self.owner_a, self.cm):
            self.project.visibility = Project.VISIBILITY.PRIVATE
            self.project.save(update_fields=['visibility'])
            response = self.set_visibility(actor, Project.VISIBILITY.PUBLIC)
            self.assertEqual(response.status_code, 200, response.content)
            self.project.refresh_from_db()
            self.assertEqual(self.project.visibility, Project.VISIBILITY.PUBLIC)

    def test_a_department_leader_cannot_publish_even_when_allowed(self):
        """A DL manages their department's projects and may take one
        company-wide, but placing company data outside the company is not a
        department-level decision."""
        self.allow_public()
        response = self.set_visibility(self.dl, Project.VISIBILITY.PUBLIC)
        self.assertEqual(response.status_code, 403)

    def test_a_department_member_cannot_publish_a_project_they_manage(self):
        """The case that motivated the gate: a DM given management of their own
        project could put it outside the tenant boundary."""
        self.allow_public()
        self.project.current_owner = self.member_a
        self.project.save(update_fields=['current_owner'])
        self.assertTrue(async_to_sync(services.user_can_manage_project)(self.member_a, self.project))

        response = self.set_visibility(self.member_a, Project.VISIBILITY.PUBLIC)
        self.assertEqual(response.status_code, 403)

    def test_the_message_says_the_company_disallows_it_not_that_you_lack_rank(self):
        """A Department Member hitting a company that does not publish should
        be told that, not that someone more senior could do it for them --
        which would be untrue and would send them to ask."""
        self.project.current_owner = self.member_a
        self.project.save(update_fields=['current_owner'])
        response = self.set_visibility(self.member_a, Project.VISIBILITY.PUBLIC)
        self.assertEqual(response.status_code, 403)
        self.assertIn('does not allow public projects', response.json()['message'])

    def test_creating_a_project_as_public_answers_to_the_same_gate(self):
        """The other way in. A gate on the transition alone would be trivially
        sidestepped by setting the visibility at creation."""
        body = {
            'title': 'Born public', 'visibility': 'public',
            'deadline': (timezone.now() + timedelta(days=90)).isoformat(),
        }
        response = self.client.post(
            '/api/v1/projects/', json.dumps(body), content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Project.objects.filter(title='Born public').exists())

        self.allow_public()
        response = self.client.post(
            '/api/v1/projects/', json.dumps(body), content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)

    # -- company visibility ------------------------------------------------

    def test_a_department_leader_can_take_their_own_department_project_company_wide(self):
        response = self.set_visibility(self.dl, Project.VISIBILITY.COMPANY)
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_department_leader_cannot_do_it_for_another_department(self):
        other_department = self.department_a.__class__.objects.create(name='Marketing', company=self.company_a)
        self.project.department = other_department
        self.project.save(update_fields=['department'])
        response = self.set_visibility(self.dl, Project.VISIBILITY.COMPANY)
        self.assertEqual(response.status_code, 403)

    def test_a_department_member_cannot_take_a_project_company_wide(self):
        self.project.current_owner = self.member_a
        self.project.save(update_fields=['current_owner'])
        response = self.set_visibility(self.member_a, Project.VISIBILITY.COMPANY)
        self.assertEqual(response.status_code, 403)

    # -- movement that stays inside the department -------------------------

    def test_moving_between_private_and_department_needs_only_management(self):
        """Both keep the project inside the department that already owns the
        work, so there is nothing here for a higher role to decide."""
        self.project.current_owner = self.member_a
        self.project.save(update_fields=['current_owner'])
        response = self.set_visibility(self.member_a, Project.VISIBILITY.DEPARTMENT)
        self.assertEqual(response.status_code, 200, response.content)

        response = self.set_visibility(self.member_a, Project.VISIBILITY.PRIVATE)
        self.assertEqual(response.status_code, 200, response.content)

    def test_lowering_visibility_is_never_gated(self):
        """Reducing exposure is always allowed to whoever manages the project.
        Gating it would mean a project could get stuck published."""
        self.allow_public()
        self.project.visibility = Project.VISIBILITY.PUBLIC
        self.project.save(update_fields=['visibility'])
        self.project.current_owner = self.member_a
        self.project.save(update_fields=['current_owner'])

        response = self.set_visibility(self.member_a, Project.VISIBILITY.PRIVATE)
        self.assertEqual(response.status_code, 200, response.content)

    # -- audit -------------------------------------------------------------

    def test_every_visibility_change_is_audited(self):
        self.allow_public()
        self.assertEqual(self.visibility_events(), [])
        self.set_visibility(self.owner_a, Project.VISIBILITY.COMPANY)
        self.set_visibility(self.owner_a, Project.VISIBILITY.PUBLIC)

        events = self.visibility_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].before['visibility'], Project.VISIBILITY.PRIVATE)
        self.assertEqual(events[0].after['visibility'], Project.VISIBILITY.COMPANY)
        self.assertEqual(events[1].after['visibility'], Project.VISIBILITY.PUBLIC)
        self.assertEqual(events[1].target_id, self.project.id)

    def test_a_refused_change_writes_no_audit_row(self):
        self.set_visibility(self.owner_a, Project.VISIBILITY.PUBLIC)
        self.assertEqual(self.visibility_events(), [])

    def test_a_no_op_change_writes_no_audit_row(self):
        """Setting the visibility it already has is not an event. An audit
        trail full of no-ops is one nobody reads."""
        response = self.set_visibility(self.owner_a, Project.VISIBILITY.PRIVATE)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.visibility_events(), [])

    # -- tenant isolation --------------------------------------------------

    def test_another_company_cannot_touch_visibility(self):
        self.allow_public()
        response = self.set_visibility(self.owner_b, Project.VISIBILITY.PUBLIC)
        self.assertIn(response.status_code, (403, 404))
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY.PRIVATE)

    def test_the_flag_is_read_from_the_projects_company_not_the_callers(self):
        """A caller whose own company allows publishing must not carry that
        permission into a company that does not."""
        self.company_b.allow_public_projects = True
        self.company_b.save(update_fields=['allow_public_projects'])
        response = self.set_visibility(self.owner_a, Project.VISIBILITY.PUBLIC)
        self.assertEqual(response.status_code, 403)


class VisibilityRequestApprovalTests(VisibilityWorldMixin, TwoCompanyTestCase):
    """Approving a request re-checks the reviewer against the target rather
    than trusting the request itself."""

    def test_approval_re_checks_authority_against_the_current_rules(self):
        self.project.created_by = self.member_a
        self.project.current_owner = self.member_a
        self.project.save(update_fields=['created_by', 'current_owner'])

        request, error = async_to_sync(services.request_visibility_change)(
            self.member_a, self.project, Project.VISIBILITY.DEPARTMENT,
        )
        self.assertIsNone(error, request)

        approved, error = async_to_sync(services.approve_visibility_request)(self.dl, request)
        self.assertIsNone(error, approved)
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY.DEPARTMENT)

    def test_approval_is_audited(self):
        self.project.created_by = self.member_a
        self.project.current_owner = self.member_a
        self.project.save(update_fields=['created_by', 'current_owner'])
        request, _ = async_to_sync(services.request_visibility_change)(
            self.member_a, self.project, Project.VISIBILITY.DEPARTMENT,
        )
        async_to_sync(services.approve_visibility_request)(self.dl, request)

        event = AuditEvent.objects.get(action=AuditAction.PROJECT_VISIBILITY_CHANGED)
        self.assertEqual(event.after['visibility'], Project.VISIBILITY.DEPARTMENT)
        self.assertIn(str(request.id), event.reason)


class DepartmentVisibilityNeedsADepartmentTests(VisibilityWorldMixin, TwoCompanyTestCase):
    """`department` visibility resolves through the project's own department.
    With none set it grants view to nobody, so the project reads as shared
    while behaving exactly like `private` -- a label that disagrees with the
    behaviour. Both ways into the state refuse it."""

    def test_a_project_with_no_department_cannot_use_department_visibility(self):
        self.project.department = None
        self.project.save(update_fields=['department'])
        response = self.set_visibility(self.owner_a, Project.VISIBILITY.DEPARTMENT)
        self.assertEqual(response.status_code, 400, response.content)
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY.PRIVATE)

    def test_creating_one_that_way_is_refused_too(self):
        response = self.client.post(
            '/api/v1/projects/', json.dumps({
                'title': 'No Department', 'visibility': Project.VISIBILITY.DEPARTMENT,
                'deadline': (timezone.now() + timedelta(days=365)).isoformat(),
            }), content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_a_departmented_project_is_unaffected(self):
        response = self.set_visibility(self.owner_a, Project.VISIBILITY.DEPARTMENT)
        self.assertEqual(response.status_code, 200, response.content)

    def test_the_refusal_writes_no_audit_row(self):
        self.project.department = None
        self.project.save(update_fields=['department'])
        self.set_visibility(self.owner_a, Project.VISIBILITY.DEPARTMENT)
        self.assertEqual(self.visibility_events(), [])


class ApprovalRefusalTests(VisibilityWorldMixin, TwoCompanyTestCase):
    """A request records what someone asked for; approving re-derives whether
    it may still happen. When it may not, the endpoint has to say so -- the
    refusal used to fall through to the success line and 500 on a None."""

    def test_approving_after_the_department_was_cleared_is_a_clean_refusal(self):
        self.project.created_by = self.member_a
        self.project.current_owner = self.member_a
        self.project.save(update_fields=['created_by', 'current_owner'])
        request, error = async_to_sync(services.request_visibility_change)(
            self.member_a, self.project, Project.VISIBILITY.DEPARTMENT,
        )
        self.assertIsNone(error, request)

        # Only an Owner/CM can do this, and it is the state that makes the
        # pending request unapprovable.
        self.project.department = None
        self.project.save(update_fields=['department'])

        response = self.client.post(
            f'/api/v1/projects/visibility-requests/{request.id}/approve/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY.PRIVATE)
        request.refresh_from_db()
        self.assertEqual(request.status, request.STATUS.PENDING)
