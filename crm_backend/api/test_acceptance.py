"""Phase 12: the full V1 product loop, exercised end-to-end through real
HTTP calls (not direct service/ORM calls) for two independent companies,
then cross-checked so no object from one leaks into the other's view.

Register -> Company -> Invite -> Project -> Manual Task -> Assignment ->
Kanban status -> AI Plan (Celery, FastAPI mocked) -> Validated Tasks ->
Notifications -> Analytics.
"""

import json
from datetime import timedelta
from unittest.mock import Mock, patch

from company.models import Sector
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from notifications_and_activity.models import Notification
from projects_and_tasks.models import Task
from users.models import User

from api.tests import auth_header

AI_PLAN_RESPONSE = {
    'success': True,
    'data': {
        'provider': 'gemini', 'model': 'gemini-flash-latest', 'summary': 'A generated plan',
        'tasks': [
            {'temporary_id': 't1', 'sequence': 1, 'title': 'Kickoff', 'estimated_effort': '2h'},
            {'temporary_id': 't2', 'sequence': 2, 'title': 'Build', 'dependency_ids': ['t1']},
        ],
    },
}


def _mock_ai_response():
    response = Mock()
    response.status_code = 200
    response.json.return_value = AI_PLAN_RESPONSE
    response.text = str(AI_PLAN_RESPONSE)
    return response


class V1AcceptanceJourneyTests(TestCase):
    def setUp(self):
        self.sector = Sector.objects.create(name='Software')

    def _post(self, url, body, user=None):
        headers = auth_header(user) if user else {}
        return self.client.post(url, json.dumps(body), content_type='application/json', **headers)

    def _run_journey(self, tag: str) -> dict:
        # 1. Register
        signup = self._post('/api/v1/auth/signup/', {
            'email': f'owner-{tag}@example.com', 'username': f'owner-{tag}', 'password': 'Kx9#mQ2vLp8Z',
        })
        self.assertEqual(signup.status_code, 201)
        owner = User.objects.get(email=f'owner-{tag}@example.com')

        # 2. Company
        register = self._post('/api/v1/company/register/', {'name': f'Acme {tag}', 'sector': str(self.sector.id)}, owner)
        self.assertEqual(register.status_code, 201)
        company_id = register.json()['data']['id']

        # 3. Invite + accept
        with patch('users.tasks.send_invitation_email') as mock_send:
            invite = self._post('/api/v1/auth/send_invite/', {'email': f'member-{tag}@example.com'}, owner)
        self.assertEqual(invite.status_code, 200)
        self.assertTrue(invite.json()['data']['email_sent'])
        raw_token = mock_send.call_args.args[-1].split('token=')[-1]
        accept = self.client.post('/api/v1/emp/accept_invite/', {
            'token': raw_token,
            'password': 'Kx9#mQ2vLp8Z',
            'full_name': f'Member {tag}',
            'profile_picture': SimpleUploadedFile(
                'avatar.png', b'\x89PNG\r\n\x1a\n', content_type='image/png',
            ),
        })
        self.assertEqual(accept.status_code, 201)
        member_id = accept.json()['data']['user']['id']
        member = User.objects.get(id=member_id)

        # 4. Project
        project = self._post('/api/v1/projects/', {
            'title': f'Project {tag}', 'visibility': 'company',
            'deadline': (timezone.now() + timedelta(days=365)).isoformat(),
        }, owner)
        self.assertEqual(project.status_code, 201)
        project_id = project.json()['data']['project']['id']

        # 5. Manual task
        task = self._post(f'/api/v1/projects/{project_id}/tasks/', {
            'title': f'Manual task {tag}', 'deadline': (timezone.now() + timedelta(days=30)).isoformat(),
        }, owner)
        self.assertEqual(task.status_code, 201)
        task_id = task.json()['data']['task']['id']

        # 6. Assignment
        assign = self._post(f'/api/v1/tasks/{task_id}/assign/', {'assigned_to_id': member_id}, owner)
        self.assertEqual(assign.status_code, 200)

        # 7. Kanban status update (assignee-only) into In Progress, then the
        # assignee submits evidence and the creator approves it -- Done is
        # only reachable through this approval workflow now, never a direct
        # status PATCH (see projects_and_tasks.services.update_task_status).
        self.client.patch(
            f'/api/v1/tasks/{task_id}/status/', json.dumps({'status': 'In Progress'}),
            content_type='application/json', **auth_header(member),
        )
        submitted = self.client.post(
            f'/api/v1/tasks/{task_id}/submit-for-approval/',
            {'links': ['https://example.com/evidence']}, **auth_header(member),
        )
        self.assertEqual(submitted.status_code, 202)
        done = self.client.post(f'/api/v1/tasks/{task_id}/approve/', **auth_header(owner))
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()['data']['task']['status'], 'Done')

        # 8. AI plan (Celery runs eagerly -- see conftest.py -- and the
        # FastAPI HTTP call is mocked, matching ai_agent/tests.py). Completion
        # only stores a draft plan for review -- nothing lands in the backlog
        # until it's explicitly saved (step 9).
        with patch('ai_agent.tasks.requests.post', return_value=_mock_ai_response()):
            ai_plan = self._post(f'/api/v1/projects/{project_id}/ai-plan/', {'prompt': f'Build a simple {tag} feature'}, owner)
        self.assertEqual(ai_plan.status_code, 202)
        generation_id = ai_plan.json()['data']['generation']['id']

        # 9. Reviewer saves the plan -- only now do real backlog Tasks exist.
        save_resp = self._post(f'/api/v1/ai/generations/{generation_id}/save/', {}, owner)
        self.assertEqual(save_resp.status_code, 200)
        ai_tasks = Task.objects.filter(project_id=project_id, source=Task.SOURCE.AI_GENERATED)
        self.assertEqual(ai_tasks.count(), 2)
        self.assertTrue(all(t.assigned_to_id is None for t in ai_tasks))

        # 10. Notifications
        owner_notifications = self.client.get('/api/v1/notifications/', **auth_header(owner)).json()['data']['results']
        member_notifications = self.client.get('/api/v1/notifications/', **auth_header(member)).json()['data']['results']
        self.assertTrue(any(n['type'] == 'ai_generation_completed' for n in owner_notifications))
        self.assertTrue(any(n['type'] == 'invitation_accepted' for n in member_notifications))
        self.assertTrue(any(n['type'] == 'task_assigned' for n in member_notifications))
        # Approval (step 7) notifies the submitting assignee, not the
        # approving creator -- see notifications_and_activity.services
        # .notify_task_approved.
        self.assertTrue(any(n['type'] == 'task_approved' for n in member_notifications))

        # 11. Analytics
        project_stats = self.client.get(f'/api/v1/analytics/projects/{project_id}/', **auth_header(owner)).json()['data']
        self.assertEqual(project_stats['total_tasks'], 3)  # 1 manual + 2 AI-generated
        self.assertEqual(project_stats['completed_tasks'], 1)
        company_stats = self.client.get('/api/v1/analytics/company/', **auth_header(owner)).json()['data']
        self.assertEqual(company_stats['project_count'], 1)
        self.assertEqual(company_stats['member_count'], 2)

        return {
            'owner': owner, 'member': member, 'company_id': company_id,
            'project_id': project_id, 'task_id': task_id, 'generation_id': generation_id,
            'owner_notification_id': owner_notifications[0]['id'],
        }

    def test_full_v1_journey_two_companies_stay_isolated(self):
        company_a = self._run_journey('a')
        company_b = self._run_journey('b')

        owner_b = company_b['owner']

        # Company B's owner must be rejected everywhere they try to touch
        # Company A's objects by id.
        project_resp = self.client.get(f"/api/v1/projects/{company_a['project_id']}/", **auth_header(owner_b))
        self.assertEqual(project_resp.status_code, 403)

        task_resp = self.client.get(f"/api/v1/tasks/{company_a['task_id']}/", **auth_header(owner_b))
        self.assertEqual(task_resp.status_code, 403)

        generation_resp = self.client.get(
            f"/api/v1/ai/generations/{company_a['generation_id']}/", **auth_header(owner_b),
        )
        self.assertEqual(generation_resp.status_code, 403)

        analytics_resp = self.client.get(
            f"/api/v1/analytics/projects/{company_a['project_id']}/", **auth_header(owner_b),
        )
        self.assertEqual(analytics_resp.status_code, 403)

        company_stats_b = self.client.get('/api/v1/analytics/company/', **auth_header(owner_b)).json()['data']
        self.assertEqual(company_stats_b['project_count'], 1)  # only company B's own project

        # Company B's owner can't read or mark-read Company A's notification.
        read_resp = self.client.post(
            f"/api/v1/notifications/{company_a['owner_notification_id']}/read/", **auth_header(owner_b),
        )
        self.assertEqual(read_resp.status_code, 404)
        self.assertTrue(Notification.objects.filter(id=company_a['owner_notification_id'], is_read=False).exists())

        # Assigning Company B's task to Company A's member must be rejected.
        cross_assign = self.client.post(
            f"/api/v1/tasks/{company_b['task_id']}/assign/",
            json.dumps({'assigned_to_id': str(company_a['member'].id)}),
            content_type='application/json', **auth_header(owner_b),
        )
        self.assertEqual(cross_assign.status_code, 400)
