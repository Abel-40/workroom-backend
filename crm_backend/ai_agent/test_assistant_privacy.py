"""AI conversations are private, and health summaries never name anyone.

Two rules from §5, and they pull in opposite directions on purpose:

- An **assistant query** is a personal question. Only the person who asked may
  read it, until they choose to share. No manager override, no owner override.
- A **health summary** is generated from analytics, not from a person's
  question, so it stays readable by anyone with project VIEW -- which is
  exactly why it must never name an individual.

The defect this closes (D8) was that assistant queries were gated on project
*view*, so every colleague who could open a project could read every question
anyone had asked about it, and a project manager could delete them.
"""

from datetime import timedelta
from unittest.mock import patch

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from django.utils import timezone
from projects_and_tasks.models import Project
from users.models import CompanyUserProfile, User

from ai_agent import assistant_services
from ai_agent.health_anonymity import names_found_in, summary_is_anonymous
from ai_agent.models import AIAssistantQuery, AIProjectHealthSummary

PASSWORD = 'Kx9#mQ2vLp8Z'


class AssistantWorldMixin:
    def setUp(self):
        super().setUp()
        self.cm = self._member('cm', CompanyUserProfile.Role.COMPANY_MANAGER)
        self.colleague = self._member('colleague')
        now = timezone.now()
        self.project = Project.objects.create(
            title='Website Revamp', company=self.company_a, department=self.department_a,
            visibility=Project.VISIBILITY.COMPANY, start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )
        self.query = self.ask(self.member_a, 'How do I scope this properly?')

    def _member(self, name, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER):
        user = User.objects.create_user(email=f'{name}@example.com', username=name, password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, department=self.department_a, role=role,
        )
        return user

    def ask(self, user, question='A question'):
        return AIAssistantQuery.objects.create(
            project=self.project, requested_by=user, question=question,
            status=AIAssistantQuery.STATUS.COMPLETED, answer='An answer.',
        )

    def read(self, actor, query=None):
        return self.client.get(
            f'/api/v1/ai/assistant-queries/{(query or self.query).id}/', **auth_header(actor),
        )

    def can_read(self, actor, query=None):
        query = query or self.query
        # Re-fetched with select_related rather than refresh_from_db(): the
        # latter clears cached relations, and the async predicate then
        # lazy-loads `project` inside the event loop -- SynchronousOnlyOperation.
        fresh = AIAssistantQuery.objects.select_related('project', 'project__company').get(id=query.id)
        return async_to_sync(assistant_services.can_read_assistant_query)(actor, fresh)


class ConversationsArePrivateTests(AssistantWorldMixin, TwoCompanyTestCase):
    """D8, closed. The rule has no exceptions, so it is tested from every
    direction somebody might expect one."""

    def test_the_person_who_asked_can_read_it(self):
        self.assertTrue(self.can_read(self.member_a))
        self.assertEqual(self.read(self.member_a).status_code, 200)

    def test_a_colleague_on_the_project_cannot(self):
        """This is the defect. Company visibility let every member read every
        question asked about the project."""
        self.assertFalse(self.can_read(self.colleague))

    def test_the_project_manager_cannot(self):
        self.assertFalse(self.can_read(self.owner_a))

    def test_the_company_manager_cannot(self):
        self.assertFalse(self.can_read(self.cm))

    def test_the_company_owner_cannot(self):
        """§5: no project-manager override, no company-owner override. What
        somebody asks an assistant is a record of what they did not know, and
        a manager able to read it changes what people are willing to ask."""
        self.assertFalse(self.can_read(self.owner_a))

    def test_another_companys_owner_cannot(self):
        self.assertFalse(self.can_read(self.owner_b))

    def test_it_answers_not_found_rather_than_forbidden(self):
        """"You may not read Alice's conversation" already tells you Alice
        asked something."""
        response = self.read(self.owner_a)
        self.assertEqual(response.status_code, 404, response.content)

    def test_a_new_query_is_private(self):
        self.assertEqual(self.query.visibility, AIAssistantQuery.Visibility.PRIVATE)

    def test_existing_rows_became_private_without_a_backfill(self):
        """The column default is the correct historical answer, so migrating
        needs no data step -- every pre-existing conversation is private."""
        field = AIAssistantQuery._meta.get_field('visibility')
        self.assertEqual(field.default, AIAssistantQuery.Visibility.PRIVATE)


class ListingDoesNotLeakTests(AssistantWorldMixin, TwoCompanyTestCase):
    def listed(self, actor):
        response = self.client.get(
            f'/api/v1/projects/{self.project.id}/ai-assistant-queries/', **auth_header(actor),
        )
        self.assertEqual(response.status_code, 200, response.content)
        return {row['id'] for row in response.json()['data']['results']}

    def test_you_see_your_own(self):
        self.assertIn(str(self.query.id), self.listed(self.member_a))

    def test_you_do_not_see_someone_elses_private_question(self):
        self.assertNotIn(str(self.query.id), self.listed(self.colleague))

    def test_not_even_the_company_owner(self):
        self.assertNotIn(str(self.query.id), self.listed(self.owner_a))

    def test_a_shared_answer_appears_for_everyone_on_the_project(self):
        async_to_sync(assistant_services.share_assistant_query)(self.member_a, self.query)
        self.assertIn(str(self.query.id), self.listed(self.colleague))
        self.assertIn(str(self.query.id), self.listed(self.owner_a))


class SharingTests(AssistantWorldMixin, TwoCompanyTestCase):
    def share(self, actor, query=None):
        return self.client.post(
            f'/api/v1/ai/assistant-queries/{(query or self.query).id}/share/', **auth_header(actor),
        )

    def test_the_owner_can_share_it_with_the_project(self):
        response = self.share(self.member_a)
        self.assertEqual(response.status_code, 200, response.content)
        self.query.refresh_from_db()
        self.assertEqual(self.query.visibility, AIAssistantQuery.Visibility.PROJECT)
        self.assertIsNotNone(self.query.shared_at)

    def test_sharing_makes_it_readable_by_project_view(self):
        self.share(self.member_a)
        self.assertTrue(self.can_read(self.colleague))
        self.assertEqual(self.read(self.colleague).status_code, 200)

    def test_a_shared_answer_is_still_not_readable_from_another_company(self):
        """Sharing widens to project VIEW, and VIEW stops at the tenant."""
        self.share(self.member_a)
        self.assertFalse(self.can_read(self.owner_b))

    def test_nobody_else_can_share_it(self):
        response = self.share(self.owner_a)
        self.assertEqual(response.status_code, 404, response.content)

    def test_a_reader_of_a_shared_answer_cannot_share_it_onward(self):
        """Being able to read does not make you the person who decides."""
        self.share(self.member_a)
        error = async_to_sync(assistant_services.share_assistant_query)(self.colleague, self.query)
        self.assertEqual(error, 'forbidden')

    def test_the_owner_can_take_it_back(self):
        self.share(self.member_a)
        response = self.client.post(
            f'/api/v1/ai/assistant-queries/{self.query.id}/unshare/', **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(self.can_read(self.colleague))

    def test_sharing_twice_is_harmless(self):
        self.share(self.member_a)
        self.assertEqual(self.share(self.member_a).status_code, 200)


class DeletionIsOwnerOnlyTests(AssistantWorldMixin, TwoCompanyTestCase):
    """§5 asks for the ability to delete somebody else's conversation to be
    removed as a code path, not guarded by a check a later change could widen."""

    def delete(self, actor, query=None):
        return self.client.delete(
            f'/api/v1/ai/assistant-queries/{(query or self.query).id}/', **auth_header(actor),
        )

    def test_the_owner_can_delete_their_own(self):
        response = self.delete(self.member_a)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(AIAssistantQuery.objects.filter(id=self.query.id).exists())

    def test_the_project_manager_cannot(self):
        response = self.delete(self.owner_a)
        self.assertEqual(response.status_code, 404, response.content)
        self.assertTrue(AIAssistantQuery.objects.filter(id=self.query.id).exists())

    def test_the_company_manager_cannot(self):
        self.delete(self.cm)
        self.assertTrue(AIAssistantQuery.objects.filter(id=self.query.id).exists())

    def test_a_manager_cannot_delete_even_a_shared_one(self):
        """Shared means readable, not disposable."""
        async_to_sync(assistant_services.share_assistant_query)(self.member_a, self.query)
        response = self.delete(self.owner_a)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertTrue(AIAssistantQuery.objects.filter(id=self.query.id).exists())

    def test_the_service_takes_no_argument_that_permits_deleting_anothers(self):
        """The code path is gone rather than gated. Stated as a test so that
        re-adding a manager branch has to break something."""
        for actor in (self.owner_a, self.cm, self.colleague, self.owner_b):
            error = async_to_sync(assistant_services.delete_assistant_query)(actor, self.query)
            self.assertEqual(error, 'forbidden')
        self.assertTrue(AIAssistantQuery.objects.filter(id=self.query.id).exists())


class HealthSummaryAnonymityTests(AssistantWorldMixin, TwoCompanyTestCase):
    """A health summary is readable by everyone with project VIEW, which is
    exactly why it must never name an individual."""

    def members(self):
        return [self.member_a, self.colleague, self.owner_a]

    def test_a_summary_about_work_is_accepted(self):
        text = 'Three tasks are overdue and the project is 60% complete. Risk is moderate.'
        self.assertTrue(summary_is_anonymous(text, self.members()))

    def test_a_first_name_is_caught(self):
        self.member_a.first_name = 'Alice'
        self.member_a.save(update_fields=['first_name'])
        text = 'Alice is behind on three tasks.'
        self.assertTrue(names_found_in(text, self.members()))

    def test_a_full_name_is_caught(self):
        self.member_a.first_name, self.member_a.last_name = 'Alice', 'Nguyen'
        self.member_a.save(update_fields=['first_name', 'last_name'])
        self.assertTrue(names_found_in('Alice Nguyen owns the delay.', self.members()))

    def test_a_username_is_caught(self):
        self.assertTrue(names_found_in('Ask colleague about the blocker.', self.members()))

    def test_an_email_local_part_is_caught(self):
        """"ask j.smith about it" names somebody just as surely as the full
        name does."""
        self.member_a.email = 'j.smith@example.com'
        self.member_a.save(update_fields=['email'])
        self.assertTrue(names_found_in('Check with j.smith on scope.', self.members()))

    def test_accents_are_not_an_escape_hatch(self):
        self.member_a.first_name = 'José'
        self.member_a.save(update_fields=['first_name'])
        self.assertTrue(names_found_in('Jose has three open tasks.', self.members()))

    def test_a_separator_variation_is_caught(self):
        self.member_a.first_name, self.member_a.last_name = 'Alice', 'Nguyen'
        self.member_a.save(update_fields=['first_name', 'last_name'])
        self.assertTrue(names_found_in('alice-nguyen is blocked.', self.members()))

    def test_a_name_inside_a_longer_word_does_not_fire(self):
        """A validator that cries wolf is one somebody disables, which is
        worse than not having it."""
        self.member_a.first_name = 'Rob'
        self.member_a.save(update_fields=['first_name'])
        self.assertFalse(names_found_in('The plan is robust and progress is steady.', self.members()))

    def test_very_short_names_are_skipped(self):
        """Initials appear inside ordinary words; matching them would reject
        every summary."""
        self.member_a.first_name = 'Al'
        self.member_a.save(update_fields=['first_name'])
        self.assertFalse(names_found_in('All tasks are on track.', self.members()))

    def test_the_company_owner_counts_as_a_member(self):
        self.owner_a.first_name = 'Bartholomew'
        self.owner_a.save(update_fields=['first_name'])
        self.assertTrue(names_found_in('Bartholomew signed it off.', self.members()))


class HealthSummaryPersistenceTests(AssistantWorldMixin, TwoCompanyTestCase):
    """The check runs before the row is written. Once it is in the table it
    has been readable."""

    def run_task_with_summary(self, summary_text, *, allow_retry=True):
        """Runs the task against a stubbed AI response.

        ``allow_retry=False`` sets max_retries to 0, which is how the
        exhausted path is reached: under eager Celery ``self.retry()`` raises
        rather than re-running, so the regenerate branch and the give-up
        branch have to be exercised separately.
        """
        from celery.exceptions import Retry

        from ai_agent.tasks_health import process_health_summary

        summary = AIProjectHealthSummary.objects.create(
            project=self.project, requested_by=self.owner_a,
            status=AIProjectHealthSummary.STATUS.PENDING,
        )
        response = {
            'data': {
                'summary': summary_text, 'risk_level': 'medium',
                'provider': 'test', 'model': 'test-model',
            },
        }
        retried = False
        with patch('ai_agent.tasks_health._call_ai_service', return_value=response):
            if not allow_retry:
                with patch.object(process_health_summary, 'max_retries', 0):
                    process_health_summary(str(summary.id))
            else:
                try:
                    process_health_summary(str(summary.id))
                except Retry:
                    retried = True
        summary.refresh_from_db()
        summary.retried = retried
        return summary

    def test_an_anonymous_summary_is_saved(self):
        summary = self.run_task_with_summary('Two tasks are overdue; delivery risk is moderate.')
        self.assertEqual(summary.status, AIProjectHealthSummary.STATUS.COMPLETED)
        self.assertIn('overdue', summary.summary)

    def test_a_summary_naming_someone_is_regenerated_not_saved(self):
        """The model is non-deterministic, so asking again is a real fix."""
        self.member_a.first_name = 'Alice'
        self.member_a.save(update_fields=['first_name'])
        summary = self.run_task_with_summary('Alice is behind on three tasks.')
        self.assertTrue(summary.retried)
        self.assertNotEqual(summary.status, AIProjectHealthSummary.STATUS.COMPLETED)
        self.assertNotIn('Alice', summary.summary)

    def test_it_fails_closed_rather_than_saving_a_cleaned_version(self):
        """Not redacted and saved -- regenerated, then failed. A summary
        stripped of names is a summary whose meaning nobody checked."""
        self.member_a.first_name = 'Alice'
        self.member_a.save(update_fields=['first_name'])
        summary = self.run_task_with_summary('Alice is behind on three tasks.', allow_retry=False)
        self.assertEqual(summary.status, AIProjectHealthSummary.STATUS.FAILED)
        self.assertEqual(summary.summary, '')
        self.assertIn('anonymously', summary.error_message)


class HealthSummariesStayVisibleTests(AssistantWorldMixin, TwoCompanyTestCase):
    """The counterpart to the privacy rule: a health summary is *not* a
    personal question, so it keeps project-VIEW visibility."""

    def test_any_project_viewer_can_read_one(self):
        summary = AIProjectHealthSummary.objects.create(
            project=self.project, requested_by=self.owner_a,
            status=AIProjectHealthSummary.STATUS.COMPLETED, summary='On track.',
        )
        response = self.client.get(
            f'/api/v1/ai/health-summaries/{summary.id}/', **auth_header(self.colleague),
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_another_company_cannot(self):
        summary = AIProjectHealthSummary.objects.create(
            project=self.project, requested_by=self.owner_a,
            status=AIProjectHealthSummary.STATUS.COMPLETED, summary='On track.',
        )
        response = self.client.get(
            f'/api/v1/ai/health-summaries/{summary.id}/', **auth_header(self.owner_b),
        )
        self.assertIn(response.status_code, (403, 404))
