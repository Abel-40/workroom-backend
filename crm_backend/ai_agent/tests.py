import uuid
from unittest.mock import Mock, patch

from company.models import Company, Sector
from departments_and_teams.models import Department
from django.test import TestCase
from notifications_and_activity.models import Notification
from projects_and_tasks.models import Project, Task, TaskType
from users.models import User

from ai_agent.models import AIGeneration
from ai_agent.tasks import process_ai_generation


def make_response(status_code, json_data):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.text = str(json_data)
    return response


class ProcessAIGenerationTests(TestCase):
    """Phase 7/8: the Celery worker calling the AI service and persisting
    the result. requests.post is mocked throughout -- these tests never hit
    a real FastAPI process or LLM provider."""

    def setUp(self):
        sector = Sector.objects.create(name='Software')
        self.owner = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')
        self.company = Company.objects.create(name='Acme', owner=self.owner, sector=sector)
        self.department = Department.objects.create(name='Engineering', company=self.company)
        self.task_type = TaskType.objects.create(name='Development', company=self.company)
        self.project = Project.objects.create(title='Support platform', company=self.company, created_by=self.owner)
        self.generation = AIGeneration.objects.create(project=self.project, requested_by=self.owner)

    def test_successful_generation_persists_tasks_and_completes(self):
        plan = {'success': True, 'data': {
            'provider': 'gemini', 'model': 'gemini-2.0-flash', 'summary': 'A plan',
            'tasks': [
                {'temporary_id': 't1', 'sequence': 1, 'title': 'Define requirements',
                 'suggested_department_id': str(self.department.id),
                 'suggested_task_type_id': str(self.task_type.id), 'estimated_effort': '4h'},
                {'temporary_id': 't2', 'sequence': 2, 'title': 'Design DB', 'dependency_ids': ['t1']},
            ],
        }}
        with patch('ai_agent.tasks.requests.post', return_value=make_response(200, plan)):
            process_ai_generation(str(self.generation.id))

        self.generation.refresh_from_db()
        self.assertEqual(self.generation.status, AIGeneration.STATUS.COMPLETED)
        self.assertEqual(self.generation.task_count, 2)
        self.assertEqual(self.generation.provider, 'gemini')

        tasks = Task.objects.filter(project=self.project)
        self.assertEqual(tasks.count(), 2)
        self.assertTrue(all(t.source == Task.SOURCE.AI_GENERATED for t in tasks))
        self.assertTrue(all(t.assigned_to_id is None for t in tasks))  # AI must never invent an assignee
        self.assertTrue(Notification.objects.filter(
            recipient=self.owner, type=Notification.Type.AI_GENERATION_COMPLETED,
        ).exists())

    def test_permanent_failure_marks_generation_failed(self):
        with patch('ai_agent.tasks.requests.post', return_value=make_response(400, {'success': False})):
            process_ai_generation(str(self.generation.id))
        self.generation.refresh_from_db()
        self.assertEqual(self.generation.status, AIGeneration.STATUS.FAILED)
        self.assertTrue(Notification.objects.filter(
            recipient=self.owner, type=Notification.Type.AI_GENERATION_FAILED,
        ).exists())

    def test_invalid_department_reference_fails_generation_without_creating_tasks(self):
        plan = {'data': {'tasks': [{
            'temporary_id': 't1', 'sequence': 1, 'title': 'X', 'suggested_department_id': str(uuid.uuid4()),
        }]}}
        with patch('ai_agent.tasks.requests.post', return_value=make_response(200, plan)):
            process_ai_generation(str(self.generation.id))
        self.generation.refresh_from_db()
        self.assertEqual(self.generation.status, AIGeneration.STATUS.FAILED)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 0)

    def test_already_completed_generation_is_not_reprocessed(self):
        self.generation.status = AIGeneration.STATUS.COMPLETED
        self.generation.save(update_fields=['status'])
        with patch('ai_agent.tasks.requests.post') as mock_post:
            process_ai_generation(str(self.generation.id))
        mock_post.assert_not_called()

    def test_transient_failure_marks_failed_once_retries_exhausted(self):
        with patch('ai_agent.tasks.requests.post', return_value=make_response(503, {})):
            process_ai_generation.push_request(retries=3)
            try:
                process_ai_generation.run(str(self.generation.id))
            finally:
                process_ai_generation.pop_request()
        self.generation.refresh_from_db()
        self.assertEqual(self.generation.status, AIGeneration.STATUS.FAILED)

    def test_transient_failure_retries_when_attempts_remain(self):
        with patch('ai_agent.tasks.requests.post', return_value=make_response(503, {})), \
             patch.object(process_ai_generation, 'retry', side_effect=RuntimeError('retry-called')) as mock_retry:
            process_ai_generation.push_request(retries=0)
            try:
                with self.assertRaises(RuntimeError):
                    process_ai_generation.run(str(self.generation.id))
            finally:
                process_ai_generation.pop_request()
        mock_retry.assert_called_once()
        self.generation.refresh_from_db()
        self.assertEqual(self.generation.status, AIGeneration.STATUS.PROCESSING)
