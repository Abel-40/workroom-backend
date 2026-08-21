import uuid
from unittest.mock import Mock, patch

from company.models import Company, Sector
from departments_and_teams.models import Department
from django.test import TestCase
from projects_and_tasks.models import Project, Task, TaskType
from users.models import User

from ai_agent.models import AIGeneratedTask, AIGeneration, AITaskContentRegeneration
from ai_agent.tasks_regenerate import process_ai_plan_regeneration, process_task_content_regeneration


def make_response(status_code, json_data):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.text = str(json_data)
    return response


class ProcessAiPlanRegenerationTests(TestCase):
    """requests.post is mocked throughout -- these tests never hit a real
    FastAPI process or LLM provider."""

    def setUp(self):
        sector = Sector.objects.create(name='Software')
        self.owner = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')
        self.company = Company.objects.create(name='Acme', owner=self.owner, sector=sector)
        self.department = Department.objects.create(name='Engineering', company=self.company)
        self.task_type = TaskType.objects.create(name='Development', company=self.company)
        self.project = Project.objects.create(title='Support platform', company=self.company, created_by=self.owner)
        self.generation = AIGeneration.objects.create(
            project=self.project, requested_by=self.owner, status=AIGeneration.STATUS.PROCESSING,
        )
        self.untouched = AIGeneratedTask.objects.create(
            generation=self.generation, temporary_id='t1', sequence=1, title='Define requirements',
        )
        self.commented = AIGeneratedTask.objects.create(
            generation=self.generation, temporary_id='t2', sequence=2, title='Design DB',
            reviewer_comment='Add indexing details.', comment_resolved=False,
        )

    def test_regeneration_updates_only_the_commented_task(self):
        response_body = {'success': True, 'data': {
            'provider': 'gemini', 'model': 'gemini-flash-latest',
            'tasks': [{'temporary_id': 't2', 'title': 'Design DB schema', 'description': 'With indexes on FKs.'}],
        }}
        with patch('ai_agent.tasks_regenerate.requests.post', return_value=make_response(200, response_body)):
            process_ai_plan_regeneration(str(self.generation.id))

        self.generation.refresh_from_db()
        self.assertEqual(self.generation.status, AIGeneration.STATUS.COMPLETED)
        self.assertEqual(self.generation.error_message, '')

        self.untouched.refresh_from_db()
        self.assertEqual(self.untouched.title, 'Define requirements')  # never touched

        self.commented.refresh_from_db()
        self.assertEqual(self.commented.title, 'Design DB schema')
        self.assertEqual(self.commented.description, 'With indexes on FKs.')
        self.assertTrue(self.commented.comment_resolved)
        self.assertEqual(self.commented.sequence, 2)  # structural fields never change
        self.assertEqual(self.commented.temporary_id, 't2')

    def test_a_missing_returned_task_reverts_to_completed_with_an_error_but_keeps_the_draft(self):
        response_body = {'success': True, 'data': {'provider': 'gemini', 'model': 'x', 'tasks': []}}
        with patch('ai_agent.tasks_regenerate.requests.post', return_value=make_response(200, response_body)):
            process_ai_plan_regeneration(str(self.generation.id))

        self.generation.refresh_from_db()
        # A failed regeneration must not destroy the already-drafted plan.
        self.assertEqual(self.generation.status, AIGeneration.STATUS.COMPLETED)
        self.assertNotEqual(self.generation.error_message, '')

        self.commented.refresh_from_db()
        self.assertFalse(self.commented.comment_resolved)  # still pending, retry is possible

    def test_invented_department_id_reverts_with_an_error(self):
        response_body = {'success': True, 'data': {'provider': 'gemini', 'model': 'x', 'tasks': [{
            'temporary_id': 't2', 'title': 'Design DB', 'suggested_department_id': str(uuid.uuid4()),
        }]}}
        with patch('ai_agent.tasks_regenerate.requests.post', return_value=make_response(200, response_body)):
            process_ai_plan_regeneration(str(self.generation.id))
        self.generation.refresh_from_db()
        self.assertEqual(self.generation.status, AIGeneration.STATUS.COMPLETED)
        self.assertNotEqual(self.generation.error_message, '')
        self.commented.refresh_from_db()
        self.assertEqual(self.commented.title, 'Design DB')  # unchanged

    def test_skipped_when_generation_is_not_processing(self):
        self.generation.status = AIGeneration.STATUS.COMPLETED
        self.generation.save(update_fields=['status'])
        with patch('ai_agent.tasks_regenerate.requests.post') as mock_post:
            process_ai_plan_regeneration(str(self.generation.id))
        mock_post.assert_not_called()

    def test_nothing_to_regenerate_reverts_to_completed_without_calling_the_ai_service(self):
        self.commented.comment_resolved = True
        self.commented.save(update_fields=['comment_resolved'])
        with patch('ai_agent.tasks_regenerate.requests.post') as mock_post:
            process_ai_plan_regeneration(str(self.generation.id))
        mock_post.assert_not_called()
        self.generation.refresh_from_db()
        self.assertEqual(self.generation.status, AIGeneration.STATUS.COMPLETED)


class ProcessTaskContentRegenerationTests(TestCase):
    def setUp(self):
        sector = Sector.objects.create(name='Software')
        self.owner = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')
        self.assignee = User.objects.create_user(email='dev@example.com', username='dev', password='Kx9#mQ2vLp8Z')
        self.company = Company.objects.create(name='Acme', owner=self.owner, sector=sector)
        self.project = Project.objects.create(title='Support platform', company=self.company, created_by=self.owner)
        self.task = Task.objects.create(
            project=self.project, title='Design DB', description='Old description.',
            created_by=self.owner, assigned_to=self.assignee, source=Task.SOURCE.AI_GENERATED,
        )
        self.regeneration = AITaskContentRegeneration.objects.create(task=self.task, requested_by=self.owner)

    def test_successful_regeneration_updates_only_the_description(self):
        response_body = {'success': True, 'data': {
            'provider': 'gemini', 'model': 'x',
            'tasks': [{'temporary_id': str(self.task.id), 'title': 'Design DB', 'description': 'New, richer description.'}],
        }}
        with patch('ai_agent.tasks_regenerate.requests.post', return_value=make_response(200, response_body)):
            process_task_content_regeneration(str(self.regeneration.id))

        self.regeneration.refresh_from_db()
        self.assertEqual(self.regeneration.status, AITaskContentRegeneration.STATUS.COMPLETED)
        self.assertEqual(self.regeneration.previous_description, 'Old description.')

        self.task.refresh_from_db()
        self.assertEqual(self.task.description, 'New, richer description.')
        # Never touches ownership/assignment/project.
        self.assertEqual(self.task.created_by_id, self.owner.id)
        self.assertEqual(self.task.assigned_to_id, self.assignee.id)
        self.assertEqual(self.task.project_id, self.project.id)

    def test_empty_returned_description_fails_the_regeneration(self):
        response_body = {'success': True, 'data': {
            'provider': 'gemini', 'model': 'x',
            'tasks': [{'temporary_id': str(self.task.id), 'title': 'Design DB', 'description': ''}],
        }}
        with patch('ai_agent.tasks_regenerate.requests.post', return_value=make_response(200, response_body)):
            process_task_content_regeneration(str(self.regeneration.id))
        self.regeneration.refresh_from_db()
        self.assertEqual(self.regeneration.status, AITaskContentRegeneration.STATUS.FAILED)
        self.task.refresh_from_db()
        self.assertEqual(self.task.description, 'Old description.')

    def test_permanent_failure_marks_the_regeneration_failed(self):
        with patch('ai_agent.tasks_regenerate.requests.post', return_value=make_response(400, {})):
            process_task_content_regeneration(str(self.regeneration.id))
        self.regeneration.refresh_from_db()
        self.assertEqual(self.regeneration.status, AITaskContentRegeneration.STATUS.FAILED)

    def test_already_completed_regeneration_is_not_reprocessed(self):
        self.regeneration.status = AITaskContentRegeneration.STATUS.COMPLETED
        self.regeneration.save(update_fields=['status'])
        with patch('ai_agent.tasks_regenerate.requests.post') as mock_post:
            process_task_content_regeneration(str(self.regeneration.id))
        mock_post.assert_not_called()

    def test_default_instructions_are_used_when_none_are_given(self):
        response_body = {'success': True, 'data': {
            'provider': 'gemini', 'model': 'x',
            'tasks': [{'temporary_id': str(self.task.id), 'title': 'Design DB', 'description': 'Refined.'}],
        }}
        with patch('ai_agent.tasks_regenerate.requests.post', return_value=make_response(200, response_body)) as mock_post:
            process_task_content_regeneration(str(self.regeneration.id))
        sent_payload = mock_post.call_args.kwargs['json']
        self.assertTrue(sent_payload['tasks_to_regenerate'][0]['reviewer_comment'])
