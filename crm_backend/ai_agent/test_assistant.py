from unittest.mock import Mock, patch

from company.models import Company, Sector
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from projects_and_tasks.models import Attachment, Project
from users.models import User

from ai_agent.models import AIAssistantQuery
from ai_agent.tasks_assistant import process_assistant_query
from utils import safe_fetch


def make_response(status_code, json_data):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.text = str(json_data)
    return response


class ProcessAssistantQueryTests(TestCase):
    """requests.post and safe_fetch are mocked throughout -- these tests
    never hit a real FastAPI process, LLM provider, or the network."""

    def setUp(self):
        sector = Sector.objects.create(name='Software')
        self.owner = User.objects.create_user(email='owner@example.com', username='owner', password='Kx9#mQ2vLp8Z')
        self.company = Company.objects.create(name='Acme', owner=self.owner, sector=sector)
        self.project = Project.objects.create(title='Support platform', company=self.company, created_by=self.owner)
        self.query = AIAssistantQuery.objects.create(
            project=self.project, requested_by=self.owner, question='What tasks are still To Do?',
        )

    def test_successful_answer_completes_query(self):
        response_body = {'success': True, 'data': {'provider': 'groq', 'model': 'groq/compound-mini', 'answer': 'You have 2 tasks left.'}}
        with patch('ai_agent.tasks_assistant.requests.post', return_value=make_response(200, response_body)):
            process_assistant_query(str(self.query.id))
        self.query.refresh_from_db()
        self.assertEqual(self.query.status, AIAssistantQuery.STATUS.COMPLETED)
        self.assertEqual(self.query.answer, 'You have 2 tasks left.')
        self.assertFalse(self.query.refused)
        self.assertEqual(self.query.provider, 'groq')

    def test_out_of_scope_sentinel_marks_refused_and_strips_prefix(self):
        response_body = {'success': True, 'data': {
            'provider': 'groq', 'model': 'groq/compound-mini',
            'answer': 'OUT_OF_SCOPE: This is unrelated to the project.',
        }}
        with patch('ai_agent.tasks_assistant.requests.post', return_value=make_response(200, response_body)):
            process_assistant_query(str(self.query.id))
        self.query.refresh_from_db()
        self.assertEqual(self.query.status, AIAssistantQuery.STATUS.COMPLETED)
        self.assertTrue(self.query.refused)
        self.assertEqual(self.query.answer, 'This is unrelated to the project.')

    def test_permanent_failure_marks_query_failed(self):
        with patch('ai_agent.tasks_assistant.requests.post', return_value=make_response(400, {'success': False})):
            process_assistant_query(str(self.query.id))
        self.query.refresh_from_db()
        self.assertEqual(self.query.status, AIAssistantQuery.STATUS.FAILED)

    def test_already_completed_query_is_not_reprocessed(self):
        self.query.status = AIAssistantQuery.STATUS.COMPLETED
        self.query.save(update_fields=['status'])
        with patch('ai_agent.tasks_assistant.requests.post') as mock_post:
            process_assistant_query(str(self.query.id))
        mock_post.assert_not_called()

    def test_transient_failure_marks_failed_once_retries_exhausted(self):
        with patch('ai_agent.tasks_assistant.requests.post', return_value=make_response(503, {})):
            process_assistant_query.push_request(retries=3)
            try:
                process_assistant_query.run(str(self.query.id))
            finally:
                process_assistant_query.pop_request()
        self.query.refresh_from_db()
        self.assertEqual(self.query.status, AIAssistantQuery.STATUS.FAILED)

    def test_transient_failure_retries_when_attempts_remain(self):
        with patch('ai_agent.tasks_assistant.requests.post', return_value=make_response(503, {})), \
             patch.object(process_assistant_query, 'retry', side_effect=RuntimeError('retry-called')) as mock_retry:
            process_assistant_query.push_request(retries=0)
            try:
                with self.assertRaises(RuntimeError):
                    process_assistant_query.run(str(self.query.id))
            finally:
                process_assistant_query.pop_request()
        mock_retry.assert_called_once()
        self.query.refresh_from_db()
        self.assertEqual(self.query.status, AIAssistantQuery.STATUS.PROCESSING)

    # --- SSRF protection (highest priority: new attack surface) ---

    def test_unsafe_reference_url_hard_fails_without_calling_ai_service(self):
        self.query.reference_url = 'http://127.0.0.1:8000/admin/'
        self.query.save(update_fields=['reference_url'])
        with patch('ai_agent.tasks_assistant.requests.post') as mock_post:
            process_assistant_query(str(self.query.id))
        mock_post.assert_not_called()
        self.query.refresh_from_db()
        self.assertEqual(self.query.status, AIAssistantQuery.STATUS.FAILED)
        self.assertIn('safely accessed', self.query.error_message)

    def test_ordinary_fetch_failure_degrades_gracefully(self):
        self.query.reference_url = 'http://example.com/unreachable'
        self.query.save(update_fields=['reference_url'])
        response_body = {'success': True, 'data': {'provider': 'groq', 'model': 'groq/compound-mini', 'answer': 'Answered from project context.'}}
        with patch('ai_agent.tasks_assistant.safe_fetch.fetch_text', side_effect=safe_fetch.FetchFailedError('timeout')), \
             patch('ai_agent.tasks_assistant.requests.post', return_value=make_response(200, response_body)) as mock_post:
            process_assistant_query(str(self.query.id))
        mock_post.assert_called_once()
        self.query.refresh_from_db()
        self.assertEqual(self.query.status, AIAssistantQuery.STATUS.COMPLETED)

    def test_redirect_to_private_ip_is_rejected(self):
        """safe_fetch itself (not mocked here) must reject a redirect chain
        that lands on a private IP even if the initial hostname resolves
        publicly -- this is a unit test of utils.safe_fetch directly."""
        def fake_getaddrinfo(hostname, *args, **kwargs):
            ip = '93.184.216.34' if hostname == 'example.com' else hostname
            return [(2, 1, 6, '', (ip, 0))]

        with patch('utils.safe_fetch.socket.getaddrinfo', side_effect=fake_getaddrinfo), \
             patch('utils.safe_fetch.requests.get') as mock_get:
            redirect_response = Mock()
            redirect_response.status_code = 302
            redirect_response.headers = {'Location': 'http://169.254.169.254/latest/meta-data/'}
            redirect_response.close = Mock()
            mock_get.return_value = redirect_response
            with self.assertRaises(safe_fetch.UnsafeURLError):
                safe_fetch.fetch_text('http://example.com/redirects-to-metadata')

    def test_document_excerpts_only_include_text_attachments(self):
        Attachment.objects.create(
            project=self.project, uploaded_by=self.owner, name='notes.txt', content_type='text/plain',
            file=SimpleUploadedFile('notes.txt', b'Some project notes.'),
        )
        Attachment.objects.create(
            project=self.project, uploaded_by=self.owner, name='image.png', content_type='image/png',
            file=SimpleUploadedFile('image.png', b'\x89PNG\r\n'),
        )
        from projects_and_tasks.services import get_text_document_excerpts
        excerpts = get_text_document_excerpts(self.project)
        self.assertEqual(len(excerpts), 1)
        self.assertIn('Some project notes.', excerpts[0])
