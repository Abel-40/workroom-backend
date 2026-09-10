"""R5: nobody signs off their own work while somebody else can.

The approval chain is unchanged and still runs task creator -> project owner
-> project creator. What changed is that a link is skipped when it *is* the
person who submitted, so the sign-off actually separates somebody from their
own work.

The reason this took a deliberate decision rather than a one-line guard: a
flat "the submitter may never approve" strands any project whose only manager
is also doing the work -- the task could never reach Done by any route, which
is the same dead end WP5 removed from late submission. So the rule is
conditional on somebody else being available, and the fallback case is
recorded rather than hidden.
"""

import json
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from audit.models import AuditAction, AuditEvent
from django.utils import timezone
from users.models import CompanyUserProfile, User

from projects_and_tasks.models import Project, Task, TaskApproval

PASSWORD = 'Kx9#mQ2vLp8Z'


class SelfApprovalFixture(TwoCompanyTestCase):
    def make_member(self, handle, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER):
        user = User.objects.create_user(
            email=f'{handle}@example.com', username=handle, password=PASSWORD,
        )
        CompanyUserProfile.objects.create(user=user, company=self.company_a, role=role)
        return user

    def make_project(self, *, created_by, current_owner):
        return Project.objects.create(
            title='Platform', company=self.company_a, created_by=created_by,
            current_owner=current_owner, visibility='company',
            deadline=timezone.now() + timedelta(days=90),
        )

    def submitted_task(self, project, *, created_by, assignee):
        """A task sitting In Review with a pending submission from
        ``assignee``."""
        task = Task.objects.create(
            project=project, title='Ship it', created_by=created_by, assigned_to=assignee,
            status=Task.STATUS.IN_REVIEW, deadline=timezone.now() + timedelta(days=10),
        )
        TaskApproval.objects.create(
            task=task, submitted_by=assignee, status=TaskApproval.STATUS.PENDING,
        )
        return task

    def approve(self, actor, task):
        return self.client.post(f'/api/v1/tasks/{task.id}/approve/', **auth_header(actor))

    def reject(self, actor, task, comment='Needs work'):
        return self.client.post(
            f'/api/v1/tasks/{task.id}/reject/', json.dumps({'comment': comment}),
            content_type='application/json', **auth_header(actor),
        )


class SelfApprovalIsRefusedWhileSomebodyElseCanTests(SelfApprovalFixture):
    def test_the_task_creator_cannot_approve_their_own_submission(self):
        """The creator heads the chain, but they submitted -- so the chain
        moves on to the project owner instead of stopping on them."""
        worker = self.make_member('worker')
        project = self.make_project(created_by=self.owner_a, current_owner=self.owner_a)
        task = self.submitted_task(project, created_by=worker, assignee=worker)

        self.assertEqual(self.approve(worker, task).status_code, 403)
        self.assertEqual(Task.objects.get(id=task.id).status, Task.STATUS.IN_REVIEW)

    def test_the_next_link_in_the_chain_can_approve_instead(self):
        worker = self.make_member('worker')
        project = self.make_project(created_by=self.owner_a, current_owner=self.owner_a)
        task = self.submitted_task(project, created_by=worker, assignee=worker)

        response = self.approve(self.owner_a, task)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Task.objects.get(id=task.id).status, Task.STATUS.DONE)

    def test_the_same_rule_applies_to_rejection(self):
        """Rejecting your own submission is the same conflict wearing a
        different hat -- it lets somebody quietly withdraw work from review."""
        worker = self.make_member('worker')
        project = self.make_project(created_by=self.owner_a, current_owner=self.owner_a)
        task = self.submitted_task(project, created_by=worker, assignee=worker)

        self.assertEqual(self.reject(worker, task).status_code, 403)
        self.assertEqual(self.reject(self.owner_a, task).status_code, 200)

    def test_a_creator_who_did_not_submit_still_approves_normally(self):
        """The ordinary case is untouched: the chain still stops on the task
        creator when they are not the one who did the work."""
        creator = self.make_member('creator')
        worker = self.make_member('worker')
        project = self.make_project(created_by=self.owner_a, current_owner=self.owner_a)
        task = self.submitted_task(project, created_by=creator, assignee=worker)

        self.assertEqual(self.approve(self.owner_a, task).status_code, 403)
        self.assertEqual(self.approve(creator, task).status_code, 200)

    def test_the_chain_order_is_unchanged(self):
        """task creator -> project owner -> project creator, preserved
        exactly. With the creator skipped as the submitter, the *owner* is
        next -- not the project's creator."""
        worker = self.make_member('worker')
        project_creator = self.make_member('project-creator')
        owner = self.make_member('project-owner')
        project = self.make_project(created_by=project_creator, current_owner=owner)
        task = self.submitted_task(project, created_by=worker, assignee=worker)

        self.assertEqual(self.approve(project_creator, task).status_code, 403)
        self.assertEqual(self.approve(owner, task).status_code, 200)


class SelfApprovalIsAllowedOnlyAsALastResortTests(SelfApprovalFixture):
    def test_a_lone_manager_can_still_close_their_own_work(self):
        """Every link in the chain is the same person, who is also the
        assignee. Refusing here would strand the task forever -- the dead end
        this rule is explicitly shaped to avoid."""
        solo = self.make_member('solo')
        project = self.make_project(created_by=solo, current_owner=solo)
        task = self.submitted_task(project, created_by=solo, assignee=solo)

        response = self.approve(solo, task)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Task.objects.get(id=task.id).status, Task.STATUS.DONE)

    def test_the_last_resort_case_is_recorded(self):
        """It is allowed, not hidden. The audit row is the only place the
        fact that nobody independent signed this off survives."""
        solo = self.make_member('solo')
        project = self.make_project(created_by=solo, current_owner=solo)
        task = self.submitted_task(project, created_by=solo, assignee=solo)
        self.approve(solo, task)

        row = AuditEvent.objects.filter(
            action=AuditAction.TASK_APPROVED, target_id=task.id,
        ).order_by('-created_at').first()
        self.assertIsNotNone(row)
        self.assertTrue(row.after['self_approved'])
        self.assertIn('no other approver', row.reason)

    def test_an_ordinary_approval_is_not_flagged_as_self_approval(self):
        worker = self.make_member('worker')
        project = self.make_project(created_by=self.owner_a, current_owner=self.owner_a)
        task = self.submitted_task(project, created_by=worker, assignee=worker)
        self.approve(self.owner_a, task)

        row = AuditEvent.objects.filter(
            action=AuditAction.TASK_APPROVED, target_id=task.id,
        ).order_by('-created_at').first()
        self.assertFalse(row.after['self_approved'])
        self.assertEqual(row.reason, '')

    def test_a_task_nobody_is_left_to_approve_stays_unapprovable(self):
        """The other way a chain empties: every link NULL because those users
        were deleted. That was unapprovable by anybody before R5 and still
        is -- the last-resort branch must not quietly widen this case."""
        worker = self.make_member('worker')
        project = self.make_project(created_by=None, current_owner=None)
        task = self.submitted_task(project, created_by=None, assignee=worker)

        self.assertEqual(self.approve(worker, task).status_code, 403)
        self.assertEqual(self.approve(self.owner_a, task).status_code, 403)


class SelfApprovalTenantIsolationTests(SelfApprovalFixture):
    def test_another_companys_owner_can_never_approve(self):
        worker = self.make_member('worker')
        project = self.make_project(created_by=self.owner_a, current_owner=self.owner_a)
        task = self.submitted_task(project, created_by=worker, assignee=worker)

        self.assertEqual(self.approve(self.owner_b, task).status_code, 403)
        self.assertEqual(Task.objects.get(id=task.id).status, Task.STATUS.IN_REVIEW)
