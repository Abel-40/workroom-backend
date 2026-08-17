from unittest.mock import Mock, patch

from company.models import Company, Sector
from django.test import TestCase
from projects_and_tasks.models import Project
from users.models import User

from ai_agent.models import AIProjectHealthSummary
from ai_agent.tasks_health import process_health_summary


def make_response(status_code, json_data):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.text = str(json_data)
    return response


class ProcessHealthSummaryTests(TestCase):
    """requests.post is mocked throughout -- these tests never hit a real
    FastAPI process or LLM provider."""

    def setUp(self):
        sector = Sector.objects.create(name='Software')
        self.owner = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')
        self.company = Company.objects.create(name='Acme', owner=self.owner, sector=sector)
        self.project = Project.objects.create(title='Support platform', company=self.company, created_by=self.owner)
        self.summary = AIProjectHealthSummary.objects.create(project=self.project, requested_by=self.owner)

    def test_successful_summary_completes(self):
        response_body = {'success': True, 'data': {
            'provider': 'gemini', 'model': 'gemini-flash-latest',
            'summary': 'The project has 3 overdue tasks and is 40% complete.', 'risk_level': 'medium',
        }}
        with patch('ai_agent.tasks_health.requests.post', return_value=make_response(200, response_body)) as mock_post:
            process_health_summary(str(self.summary.id))
        self.summary.refresh_from_db()
        self.assertEqual(self.summary.status, AIProjectHealthSummary.STATUS.COMPLETED)
        self.assertEqual(self.summary.risk_level, 'medium')
        self.assertIn('overdue', self.summary.summary)
        # Stats sent to the AI service must include the new unassigned_tasks key.
        sent_payload = mock_post.call_args.kwargs['json']
        self.assertIn('unassigned_tasks', sent_payload['stats'])

    def test_invalid_risk_level_is_defensively_normalized(self):
        response_body = {'success': True, 'data': {
            'provider': 'gemini', 'model': 'gemini-flash-latest',
            'summary': 'Some summary.', 'risk_level': 'catastrophic',
        }}
        with patch('ai_agent.tasks_health.requests.post', return_value=make_response(200, response_body)):
            process_health_summary(str(self.summary.id))
        self.summary.refresh_from_db()
        self.assertEqual(self.summary.status, AIProjectHealthSummary.STATUS.COMPLETED)
        self.assertEqual(self.summary.risk_level, '')

    def test_permanent_failure_marks_summary_failed(self):
        with patch('ai_agent.tasks_health.requests.post', return_value=make_response(400, {'success': False})):
            process_health_summary(str(self.summary.id))
        self.summary.refresh_from_db()
        self.assertEqual(self.summary.status, AIProjectHealthSummary.STATUS.FAILED)

    def test_already_completed_summary_is_not_reprocessed(self):
        self.summary.status = AIProjectHealthSummary.STATUS.COMPLETED
        self.summary.save(update_fields=['status'])
        with patch('ai_agent.tasks_health.requests.post') as mock_post:
            process_health_summary(str(self.summary.id))
        mock_post.assert_not_called()

    def test_transient_failure_marks_failed_once_retries_exhausted(self):
        with patch('ai_agent.tasks_health.requests.post', return_value=make_response(503, {})):
            process_health_summary.push_request(retries=3)
            try:
                process_health_summary.run(str(self.summary.id))
            finally:
                process_health_summary.pop_request()
        self.summary.refresh_from_db()
        self.assertEqual(self.summary.status, AIProjectHealthSummary.STATUS.FAILED)

    def test_transient_failure_retries_when_attempts_remain(self):
        with patch('ai_agent.tasks_health.requests.post', return_value=make_response(503, {})), \
             patch.object(process_health_summary, 'retry', side_effect=RuntimeError('retry-called')) as mock_retry:
            process_health_summary.push_request(retries=0)
            try:
                with self.assertRaises(RuntimeError):
                    process_health_summary.run(str(self.summary.id))
            finally:
                process_health_summary.pop_request()
        mock_retry.assert_called_once()
        self.summary.refresh_from_db()
        self.assertEqual(self.summary.status, AIProjectHealthSummary.STATUS.PROCESSING)
