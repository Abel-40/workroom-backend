"""Creating a task requires MANAGE; everyone else proposes one.

Project *view* used to be enough to add a task, which meant `company`
visibility -- meant to grant discovery and nothing else -- let any member of
the company add work to any project they could find, and pick who it was
assigned to. That is D1, the last place visibility still conferred a
capability.

Tightening it alone would have left contributors no way to raise work at all,
so the replacement lands in the same package: a proposal is an
`ApprovalRequest`, and somebody with MANAGE turns it into a real task through
`create_task` -- never by writing the payload straight into the tasks table.
"""

import json
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from audit.models import AuditAction, AuditEvent
from django.utils import timezone
from notifications_and_activity.models import Notification
from users.models import CompanyUserProfile, User

from projects_and_tasks import services
from projects_and_tasks.models import ApprovalRequest, Project, ProjectMembership, Task

PASSWORD = 'Kx9#mQ2vLp8Z'


class ProposalWorldMixin:
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.project = Project.objects.create(
            title='Website Revamp', company=self.company_a, department=self.department_a,
            visibility=Project.VISIBILITY.COMPANY, start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )
        self.contributor = self._member('contributor')
        ProjectMembership.objects.create(
            project=self.project, user=self.contributor,
            role=ProjectMembership.Role.CONTRIBUTOR, added_by=self.owner_a,
        )
        # Company visibility alone -- discovery, nothing more.
        self.viewer = self._member('viewer')

    def _member(self, name, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER, department=None):
        user = User.objects.create_user(email=f'{name}@example.com', username=name, password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, department=department or self.department_a, role=role,
        )
        return user

    def create_task_via_api(self, actor, **overrides):
        body = {
            'title': 'Write the brief', 'description': 'Draft it', 'priority': 'medium',
            'deadline': (self.project.deadline - timedelta(days=1)).isoformat(),
        }
        body.update(overrides)
        return self.client.post(
            f'/api/v1/projects/{self.project.id}/tasks/', json.dumps(body),
            content_type='application/json', **auth_header(actor),
        )

    def propose(self, actor, **overrides):
        body = {'title': 'Add a pricing page', 'description': 'We keep being asked for one'}
        body.update(overrides)
        return self.client.post(
            f'/api/v1/projects/{self.project.id}/task-proposals/', json.dumps(body),
            content_type='application/json', **auth_header(actor),
        )

    def accept(self, actor, proposal_id, **overrides):
        return self.client.post(
            f'/api/v1/task-proposals/{proposal_id}/accept/', json.dumps(overrides),
            content_type='application/json', **auth_header(actor),
        )

    def decline(self, actor, proposal_id, comment=''):
        return self.client.post(
            f'/api/v1/task-proposals/{proposal_id}/decline/', json.dumps({'comment': comment}),
            content_type='application/json', **auth_header(actor),
        )


class TaskCreationRequiresManageTests(ProposalWorldMixin, TwoCompanyTestCase):
    """FIXED (was D1): visibility grants discovery, never the ability to add
    work."""

    def test_a_manager_can_still_create_directly(self):
        response = self.create_task_via_api(self.owner_a)
        self.assertEqual(response.status_code, 201, response.content)

    def test_a_contributor_cannot_create_directly(self):
        response = self.create_task_via_api(self.contributor)
        self.assertEqual(response.status_code, 403, response.content)

    def test_someone_with_only_company_visibility_cannot_create(self):
        response = self.create_task_via_api(self.viewer)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    def test_the_refusal_names_the_route_that_does_work(self):
        """A 403 that only says no would leave a contributor with nowhere to
        go, which is how the old permissive rule got written in the first
        place."""
        response = self.create_task_via_api(self.contributor)
        self.assertIn('task-proposals', response.json()['message'])

    def test_another_companys_owner_cannot_create(self):
        response = self.create_task_via_api(self.owner_b)
        self.assertIn(response.status_code, (403, 404))


class ProposingTests(ProposalWorldMixin, TwoCompanyTestCase):
    def test_a_contributor_can_propose(self):
        response = self.propose(self.contributor)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(
            ApprovalRequest.objects.filter(
                kind=ApprovalRequest.Kind.TASK_PROPOSAL, target_id=self.project.id,
            ).exists()
        )

    def test_a_proposal_is_not_a_task(self):
        """The whole reason a proposal is an ApprovalRequest: an unaccepted
        one must not appear on the board or count toward progress."""
        self.propose(self.contributor)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    def test_a_manager_is_told_to_create_it_directly(self):
        """Not a permission failure -- accepting it would queue a manager's
        proposal for the manager who filed it."""
        response = self.propose(self.owner_a)
        self.assertEqual(response.status_code, 400, response.content)

    def test_someone_with_only_visibility_cannot_propose(self):
        """Proposing needs CONTRIBUTE. Discovery alone is not a licence to put
        things in someone else's review queue."""
        response = self.propose(self.viewer)
        self.assertEqual(response.status_code, 403, response.content)

    def test_several_contributors_may_each_hold_open_proposals(self):
        """The pending-uniqueness constraint is scoped to visibility requests
        for exactly this reason -- a blanket one would have let a single
        person hold a proposal open at a time, per project."""
        second = self._member('second-contributor')
        ProjectMembership.objects.create(
            project=self.project, user=second, role=ProjectMembership.Role.CONTRIBUTOR, added_by=self.owner_a,
        )
        self.assertEqual(self.propose(self.contributor, title='One').status_code, 201)
        self.assertEqual(self.propose(self.contributor, title='Two').status_code, 201)
        self.assertEqual(self.propose(second, title='Three').status_code, 201)
        self.assertEqual(
            ApprovalRequest.objects.filter(
                kind=ApprovalRequest.Kind.TASK_PROPOSAL, status=ApprovalRequest.Status.PENDING,
            ).count(),
            3,
        )

    def test_a_deadline_past_the_projects_is_refused_at_proposal_time(self):
        """Refused where somebody can still fix it, rather than surfacing as a
        confusing failure for the reviewer days later."""
        response = self.propose(
            self.contributor, deadline=(self.project.deadline + timedelta(days=5)).isoformat(),
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_the_reviewer_is_notified(self):
        self.propose(self.contributor)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.owner_a, type=Notification.Type.TASK_PROPOSED,
            ).exists()
        )

    def test_a_proposal_cannot_name_an_assignee(self):
        """Deciding who does the work is a management act. The schema has no
        assigned_to_id, so a client sending one has it dropped rather than
        honoured."""
        response = self.propose(self.contributor, assigned_to_id=str(self.contributor.id))
        self.assertEqual(response.status_code, 201, response.content)
        proposal = ApprovalRequest.objects.get(kind=ApprovalRequest.Kind.TASK_PROPOSAL)
        self.assertNotIn('assigned_to_id', proposal.payload)


class AcceptingTests(ProposalWorldMixin, TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.propose(self.contributor)
        self.proposal = ApprovalRequest.objects.get(kind=ApprovalRequest.Kind.TASK_PROPOSAL)

    def test_a_manager_can_accept_and_it_becomes_a_task(self):
        response = self.accept(self.owner_a, self.proposal.id)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(Task.objects.filter(project=self.project, title='Add a pricing page').exists())

    def test_the_proposer_cannot_accept_their_own(self):
        response = self.accept(self.contributor, self.proposal.id)
        self.assertEqual(response.status_code, 403, response.content)

    def test_created_by_is_the_accepting_manager_not_the_proposer(self):
        """created_by heads the approval fallback chain in
        user_can_approve_task. Pointing it at the proposer would make a
        contributor the approver of work they suggested and may be assigned."""
        self.accept(self.owner_a, self.proposal.id)
        task = Task.objects.get(project=self.project)
        self.assertEqual(task.created_by_id, self.owner_a.id)

    def test_the_proposer_stays_recorded_on_the_request(self):
        """Provenance is not lost by the above -- it moves to where it belongs."""
        self.accept(self.owner_a, self.proposal.id)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.requested_by_id, self.contributor.id)

    def test_a_reviewer_may_correct_the_proposal_as_they_accept(self):
        response = self.accept(self.owner_a, self.proposal.id, title='Pricing page', priority='high')
        self.assertEqual(response.status_code, 201, response.content)
        task = Task.objects.get(project=self.project)
        self.assertEqual(task.title, 'Pricing page')
        self.assertEqual(task.priority, Task.PRIORITY.HIGH)

    def test_accepting_twice_is_refused(self):
        self.accept(self.owner_a, self.proposal.id)
        response = self.accept(self.owner_a, self.proposal.id)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)

    def test_acceptance_revalidates_against_the_project_as_it_is_now(self):
        """The payload is an ask, not an authorization. A proposal written
        against one project deadline must not become a task that violates the
        deadline the project has now."""
        self.project.deadline = timezone.now() + timedelta(days=1)
        self.project.save(update_fields=['deadline'])
        self.proposal.payload['deadline'] = (timezone.now() + timedelta(days=60)).isoformat()
        self.proposal.save(update_fields=['payload'])

        response = self.accept(self.owner_a, self.proposal.id)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    def test_the_proposer_is_told_it_was_accepted(self):
        self.accept(self.owner_a, self.proposal.id)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.contributor, type=Notification.Type.TASK_PROPOSAL_ACCEPTED,
            ).exists()
        )

    def test_the_decision_is_audited(self):
        before = AuditEvent.objects.filter(action=AuditAction.TASK_PROPOSAL_DECIDED).count()
        self.accept(self.owner_a, self.proposal.id)
        after = AuditEvent.objects.filter(action=AuditAction.TASK_PROPOSAL_DECIDED).count()
        self.assertEqual(after, before + 1)

    def test_another_companys_owner_cannot_reach_the_proposal(self):
        response = self.accept(self.owner_b, self.proposal.id)
        self.assertEqual(response.status_code, 404, response.content)
        self.assertFalse(Task.objects.filter(project=self.project).exists())


class DecliningTests(ProposalWorldMixin, TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.propose(self.contributor)
        self.proposal = ApprovalRequest.objects.get(kind=ApprovalRequest.Kind.TASK_PROPOSAL)

    def test_a_manager_can_decline_with_a_reason(self):
        response = self.decline(self.owner_a, self.proposal.id, comment='Already covered by the CMS work')
        self.assertEqual(response.status_code, 200, response.content)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ApprovalRequest.Status.DENIED)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    def test_the_proposer_is_told_and_gets_the_reason(self):
        """Unlike a rejected task submission -- where the comment is private
        to the submitter and withheld from every other read path -- a declined
        proposal has exactly one audience, and withholding the reason would
        leave them guessing about work they still think needs doing."""
        self.decline(self.owner_a, self.proposal.id, comment='Already covered by the CMS work')
        notification = Notification.objects.get(
            recipient=self.contributor, type=Notification.Type.TASK_PROPOSAL_DECLINED,
        )
        self.assertIn('CMS work', notification.message)

    def test_the_proposer_cannot_decline_their_own(self):
        response = self.decline(self.contributor, self.proposal.id)
        self.assertEqual(response.status_code, 403, response.content)

    def test_declining_twice_is_refused(self):
        self.decline(self.owner_a, self.proposal.id)
        response = self.decline(self.owner_a, self.proposal.id)
        self.assertEqual(response.status_code, 400, response.content)

    def test_a_declined_proposal_frees_nothing_and_blocks_nothing(self):
        """No pending-uniqueness applies to proposals, so this is really a
        check that declining does not somehow prevent asking again."""
        self.decline(self.owner_a, self.proposal.id)
        self.assertEqual(self.propose(self.contributor).status_code, 201)


class ProposalVisibilityTests(ProposalWorldMixin, TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.propose(self.contributor)

    def list_proposals(self, actor):
        return self.client.get(
            f'/api/v1/projects/{self.project.id}/task-proposals/', **auth_header(actor)
        )

    def test_a_manager_sees_every_pending_proposal(self):
        response = self.list_proposals(self.owner_a)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()['data']['results']), 1)

    def test_a_proposer_sees_their_own(self):
        """Not being able to see what you asked for is how people ask twice."""
        response = self.list_proposals(self.contributor)
        self.assertEqual(len(response.json()['data']['results']), 1)

    def test_someone_elses_proposal_is_not_visible_to_a_non_manager(self):
        other = self._member('other-contributor')
        ProjectMembership.objects.create(
            project=self.project, user=other, role=ProjectMembership.Role.CONTRIBUTOR, added_by=self.owner_a,
        )
        response = self.list_proposals(other)
        self.assertEqual(len(response.json()['data']['results']), 0)


class PayloadIsNotTrustedTests(ProposalWorldMixin, TwoCompanyTestCase):
    """`ApprovalRequest`'s core rule, tested directly: approving applies a
    change through the same validated path a direct action takes."""

    def test_a_payload_field_outside_the_allow_list_never_reaches_the_task(self):
        self.propose(self.contributor)
        proposal = ApprovalRequest.objects.get(kind=ApprovalRequest.Kind.TASK_PROPOSAL)
        proposal.payload['assigned_to_id'] = str(self.contributor.id)
        proposal.payload['status'] = Task.STATUS.DONE
        proposal.save(update_fields=['payload'])

        self.accept(self.owner_a, proposal.id)
        task = Task.objects.get(project=self.project)
        self.assertIsNone(task.assigned_to_id)
        self.assertEqual(task.status, Task.STATUS.TODO)

    def test_a_proposal_for_a_deleted_project_cannot_be_accepted(self):
        self.propose(self.contributor)
        proposal = ApprovalRequest.objects.get(kind=ApprovalRequest.Kind.TASK_PROPOSAL)
        self.project.is_deleted = True
        self.project.save(update_fields=['is_deleted'])

        response = self.accept(self.owner_a, proposal.id)
        self.assertEqual(response.status_code, 404, response.content)

    def test_the_service_refuses_a_proposal_on_a_completed_project(self):
        done_project = Project.objects.create(
            title='Finished', company=self.company_a, department=self.department_a,
            visibility=Project.VISIBILITY.COMPANY, start_date=timezone.now(),
            deadline=timezone.now() + timedelta(days=10), status=Project.STATUS.DONE,
            created_by=self.owner_a, current_owner=self.owner_a,
        )
        ProjectMembership.objects.create(
            project=done_project, user=self.contributor,
            role=ProjectMembership.Role.CONTRIBUTOR, added_by=self.owner_a,
        )
        proposal, error = async_to_sync(services.propose_task)(
            self.contributor, done_project, title='Too late',
        )
        self.assertEqual(error, 'project_completed')
        self.assertIsNone(proposal)
