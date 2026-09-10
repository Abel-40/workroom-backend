"""The AI-assisted brief route and its two human gates (§1).

The property worth defending, and the reason the feature is shaped this way
at all: **the AI never writes project state.** An extraction sits on the
assist row until a person reads it, edits it, and confirms -- and even then it
is applied through the same validated path the guided form uses.

Celery runs eagerly in tests (conftest.py), so posting an extraction executes
the worker body inline. The AI service is always stubbed at the requests.post
boundary; no test here reaches a real provider.
"""

import json
from datetime import timedelta
from unittest.mock import patch

from api.tests import TwoCompanyTestCase, auth_header
from django.utils import timezone
from projects_and_tasks.models import Project, ProjectBrief

from ai_agent.models import BriefAssist

PASSWORD = 'Kx9#mQ2vLp8Z'

SOURCE = 'We must replace the checkout flow. It currently loses 30% of carts.'


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def extraction_ok(**overrides):
    data = {
        'provider': 'gemini', 'model': 'gemini-2.0-flash',
        'objective': 'Replace the checkout flow.',
        'background': 'It loses 30% of carts.',
        'scope_in': 'Cart, payment', 'scope_out': 'Mobile app',
        'expected_outcome': 'Abandonment below 15%.', 'constraints': 'No downtime.',
        'missing_fields': [],
        'usage': {'input_tokens': 400, 'output_tokens': 120},
    }
    data.update(overrides)
    return FakeResponse(200, {'success': True, 'data': data})


def interpretation_ok(**overrides):
    data = {
        'provider': 'gemini', 'model': 'gemini-2.0-flash',
        'restated_objective': 'Rebuild checkout to reduce abandonment.',
        'assumptions': ['The payment provider stays'],
        'open_questions': ['Which countries at launch?'],
        'usage': {'input_tokens': 200, 'output_tokens': 80},
    }
    data.update(overrides)
    return FakeResponse(200, {'success': True, 'data': data})


class BriefAssistFixture(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            title='Checkout Rebuild', company=self.company_a, created_by=self.owner_a,
            current_owner=self.owner_a, visibility='company',
            deadline=timezone.now() + timedelta(days=90),
        )

    def start(self, actor=None, source_text=SOURCE, response=None):
        with patch('ai_agent.tasks_brief.requests.post', return_value=response or extraction_ok()):
            return self.client.post(
                f'/api/v1/projects/{self.project.id}/brief/extract/',
                json.dumps({'source_text': source_text}), content_type='application/json',
                **auth_header(actor or self.owner_a),
            )

    def confirm_extraction(self, assist_id, actor=None, response=None, **overrides):
        with patch('ai_agent.tasks_brief.requests.post', return_value=response or interpretation_ok()):
            return self.client.post(
                f'/api/v1/projects/{self.project.id}/brief/assist/{assist_id}/confirm-extraction/',
                json.dumps(overrides), content_type='application/json',
                **auth_header(actor or self.owner_a),
            )

    def confirm_interpretation(self, assist_id, actor=None):
        return self.client.post(
            f'/api/v1/projects/{self.project.id}/brief/assist/{assist_id}/confirm-interpretation/',
            **auth_header(actor or self.owner_a),
        )

    def run_to_ready(self):
        assist_id = self.start().json()['data']['assist']['id']
        self.confirm_extraction(assist_id)
        self.confirm_interpretation(assist_id)
        return assist_id


class ExtractionTests(BriefAssistFixture):
    def test_extraction_fills_the_assist_but_writes_no_brief(self):
        """The property the whole feature is shaped around: nothing reaches
        the project until a person confirms."""
        response = self.start()
        self.assertEqual(response.status_code, 202, response.content)
        assist = BriefAssist.objects.get()
        self.assertEqual(assist.status, BriefAssist.STATUS.EXTRACTED)
        self.assertEqual(assist.extracted['objective'], 'Replace the checkout flow.')
        self.assertFalse(ProjectBrief.objects.filter(project=self.project).exists())

    def test_usage_is_recorded(self):
        self.start()
        assist = BriefAssist.objects.get()
        self.assertEqual(assist.input_tokens, 400)
        self.assertEqual(assist.output_tokens, 120)
        self.assertIsNotNone(assist.cost)

    def test_the_document_text_is_never_stored(self):
        """Only its name and length. A brief is the artifact worth keeping;
        keeping the upload would make this a second document store."""
        self.start()
        assist = BriefAssist.objects.get()
        self.assertEqual(assist.source_chars, len(SOURCE))
        for value in assist.__dict__.values():
            if isinstance(value, str):
                self.assertNotIn('loses 30% of carts', value)

    def test_a_provider_failure_fails_the_assist_without_touching_the_brief(self):
        self.start(response=FakeResponse(400, {'success': False}))
        assist = BriefAssist.objects.get()
        self.assertEqual(assist.status, BriefAssist.STATUS.FAILED)
        self.assertFalse(ProjectBrief.objects.filter(project=self.project).exists())

    def test_a_second_request_while_one_is_in_flight_returns_the_first(self):
        BriefAssist.objects.create(
            project=self.project, requested_by=self.owner_a, status=BriefAssist.STATUS.EXTRACTING,
        )
        with patch('ai_agent.tasks_brief.requests.post') as post:
            response = self.client.post(
                f'/api/v1/projects/{self.project.id}/brief/extract/',
                json.dumps({'source_text': SOURCE}), content_type='application/json',
                **auth_header(self.owner_a),
            )
        self.assertEqual(response.status_code, 202)
        post.assert_not_called()
        self.assertEqual(BriefAssist.objects.count(), 1)

    def test_empty_source_is_refused(self):
        response = self.client.post(
            f'/api/v1/projects/{self.project.id}/brief/extract/',
            json.dumps({'source_text': '   '}), content_type='application/json',
            **auth_header(self.owner_a),
        )
        # 400, not 422: whitespace passes the schema's min_length and is
        # rejected by the service, which can say *why* rather than just
        # "invalid".
        self.assertEqual(response.status_code, 400)

    def test_manage_is_required(self):
        response = self.start(actor=self.member_a)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BriefAssist.objects.exists())

    def test_cross_tenant_is_refused(self):
        response = self.start(actor=self.owner_b)
        self.assertEqual(response.status_code, 403)


class GateOneTests(BriefAssistFixture):
    def test_confirming_writes_the_brief_and_starts_interpretation(self):
        assist_id = self.start().json()['data']['assist']['id']
        response = self.confirm_extraction(assist_id)
        self.assertEqual(response.status_code, 200, response.content)

        brief = ProjectBrief.objects.get(project=self.project)
        self.assertEqual(brief.objective, 'Replace the checkout flow.')
        assist = BriefAssist.objects.get()
        self.assertIsNotNone(assist.extraction_confirmed_at)
        self.assertEqual(assist.status, BriefAssist.STATUS.INTERPRETED)

    def test_the_reviewers_edits_win_over_the_model(self):
        """The entire point of the gate. What gets written is what a person
        agreed to, which may be nothing the model said."""
        assist_id = self.start().json()['data']['assist']['id']
        self.confirm_extraction(assist_id, objective='Actually: rebuild the cart, not checkout.')
        brief = ProjectBrief.objects.get(project=self.project)
        self.assertEqual(brief.objective, 'Actually: rebuild the cart, not checkout.')
        # Unedited fields still come from the extraction.
        self.assertEqual(brief.scope_out, 'Mobile app')

    def test_confirming_twice_is_refused(self):
        assist_id = self.start().json()['data']['assist']['id']
        self.confirm_extraction(assist_id)
        self.assertEqual(self.confirm_extraction(assist_id).status_code, 400)

    def test_manage_is_required_to_confirm(self):
        assist_id = self.start().json()['data']['assist']['id']
        response = self.confirm_extraction(assist_id, actor=self.member_a)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ProjectBrief.objects.filter(project=self.project).exists())

    def test_cross_tenant_confirm_is_refused(self):
        assist_id = self.start().json()['data']['assist']['id']
        self.assertEqual(self.confirm_extraction(assist_id, actor=self.owner_b).status_code, 403)


class GateTwoTests(BriefAssistFixture):
    def test_interpretation_is_produced_and_confirmable(self):
        assist_id = self.start().json()['data']['assist']['id']
        self.confirm_extraction(assist_id)
        assist = BriefAssist.objects.get()
        self.assertEqual(assist.interpretation['open_questions'], ['Which countries at launch?'])

        response = self.confirm_interpretation(assist_id)
        self.assertEqual(response.status_code, 200, response.content)
        assist.refresh_from_db()
        self.assertEqual(assist.status, BriefAssist.STATUS.READY)
        self.assertIsNotNone(assist.interpretation_confirmed_at)

    def test_the_interpretation_reads_the_confirmed_brief_not_the_raw_extraction(self):
        """Gate 1's edits are what gets interpreted, so gate 2 is answering
        for the text that will actually be planned from."""
        assist_id = self.start().json()['data']['assist']['id']
        # The endpoint is called directly rather than through the helper: the
        # helper opens its own patch on the same target, and a nested patch
        # would shadow this one so the outer mock recorded nothing.
        with patch('ai_agent.tasks_brief.requests.post', return_value=interpretation_ok()) as post:
            self.client.post(
                f'/api/v1/projects/{self.project.id}/brief/assist/{assist_id}/confirm-extraction/',
                json.dumps({'objective': 'Rebuild the cart.'}), content_type='application/json',
                **auth_header(self.owner_a),
            )
        sent = post.call_args.kwargs['json']
        self.assertEqual(sent['objective'], 'Rebuild the cart.')

    def test_confirming_out_of_order_is_refused(self):
        assist_id = self.start().json()['data']['assist']['id']
        # Still EXTRACTED -- gate 1 has not been passed.
        self.assertEqual(self.confirm_interpretation(assist_id).status_code, 400)

    def test_manage_is_required(self):
        assist_id = self.start().json()['data']['assist']['id']
        self.confirm_extraction(assist_id)
        self.assertEqual(self.confirm_interpretation(assist_id, actor=self.member_a).status_code, 403)


class PlanningIsGatedTests(BriefAssistFixture):
    """§1's "then and only then"."""

    def request_plan(self, actor=None):
        with patch('ai_agent.services.process_ai_generation.delay'):
            return self.client.post(
                f'/api/v1/projects/{self.project.id}/ai-plan/',
                json.dumps({'prompt': 'Build it'}), content_type='application/json',
                **auth_header(actor or self.owner_a),
            )

    def test_planning_is_refused_while_an_extraction_awaits_confirmation(self):
        self.start()
        response = self.request_plan()
        self.assertEqual(response.status_code, 400)
        self.assertIn('brief', response.json()['message'].lower())

    def test_planning_is_refused_while_an_interpretation_awaits_confirmation(self):
        assist_id = self.start().json()['data']['assist']['id']
        self.confirm_extraction(assist_id)
        self.assertEqual(self.request_plan().status_code, 400)

    def test_planning_is_allowed_once_both_gates_are_passed(self):
        self.run_to_ready()
        self.assertEqual(self.request_plan().status_code, 202)

    def test_a_project_that_never_used_the_assisted_route_is_not_gated(self):
        """The guided form is still the default way in, and it needs no gate
        -- a person wrote every word of it themselves."""
        self.assertFalse(BriefAssist.objects.exists())
        self.assertEqual(self.request_plan().status_code, 202)

    def test_a_failed_assist_does_not_block_planning_forever(self):
        """A provider failure must not strand the project. FAILED is terminal
        and not one of the awaiting-confirmation states."""
        self.start(response=FakeResponse(400, {'success': False}))
        self.assertEqual(BriefAssist.objects.get().status, BriefAssist.STATUS.FAILED)
        self.assertEqual(self.request_plan().status_code, 202)


class AssistVisibilityTests(BriefAssistFixture):
    def test_the_status_endpoint_reports_progress(self):
        self.start()
        response = self.client.get(
            f'/api/v1/projects/{self.project.id}/brief/assist/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['assist']['status'], BriefAssist.STATUS.EXTRACTED)

    def test_a_project_with_no_assist_reports_404(self):
        response = self.client.get(
            f'/api/v1/projects/{self.project.id}/brief/assist/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_tenant_status_is_refused(self):
        self.start()
        response = self.client.get(
            f'/api/v1/projects/{self.project.id}/brief/assist/', **auth_header(self.owner_b),
        )
        self.assertEqual(response.status_code, 403)
