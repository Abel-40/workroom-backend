"""§4 re-validation: the DAG check, capacity flagging on suggested assignees,
and idempotency/in-flight protection for plan requests.

Every test here mocks ``requests.post`` and never talks to the real FastAPI
service or an LLM -- same convention as ``ai_agent/tests.py``.
"""

from datetime import timedelta
from unittest.mock import Mock, patch

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone
from projects_and_tasks.models import Project
from workforce.models import AssignmentPolicy

from ai_agent import services
from ai_agent.models import AIGeneratedTask, AIGeneration
from ai_agent.tasks import _assert_plan_is_a_dag, process_ai_generation


def make_response(status_code, json_data):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.text = str(json_data)
    return response


class DagCheckTests(TestCase):
    """Pure function -- no database needed."""

    def test_a_linear_chain_is_fine(self):
        _assert_plan_is_a_dag([
            {'temporary_id': 't1', 'dependency_ids': []},
            {'temporary_id': 't2', 'dependency_ids': ['t1']},
            {'temporary_id': 't3', 'dependency_ids': ['t2']},
        ])  # does not raise

    def test_a_diamond_is_fine(self):
        _assert_plan_is_a_dag([
            {'temporary_id': 't1', 'dependency_ids': []},
            {'temporary_id': 't2', 'dependency_ids': ['t1']},
            {'temporary_id': 't3', 'dependency_ids': ['t1']},
            {'temporary_id': 't4', 'dependency_ids': ['t2', 't3']},
        ])  # does not raise

    def test_a_direct_cycle_is_rejected(self):
        with self.assertRaises(ValueError):
            _assert_plan_is_a_dag([
                {'temporary_id': 't1', 'dependency_ids': ['t2']},
                {'temporary_id': 't2', 'dependency_ids': ['t1']},
            ])

    def test_a_longer_cycle_is_rejected(self):
        with self.assertRaises(ValueError):
            _assert_plan_is_a_dag([
                {'temporary_id': 't1', 'dependency_ids': ['t3']},
                {'temporary_id': 't2', 'dependency_ids': ['t1']},
                {'temporary_id': 't3', 'dependency_ids': ['t2']},
            ])

    def test_a_self_dependency_is_rejected(self):
        with self.assertRaises(ValueError):
            _assert_plan_is_a_dag([{'temporary_id': 't1', 'dependency_ids': ['t1']}])

    def test_an_isolated_cycle_alongside_valid_tasks_is_still_rejected(self):
        """The plan is one unit -- a cycle anywhere in it fails the whole
        generation, not just the tasks caught in the loop."""
        with self.assertRaises(ValueError):
            _assert_plan_is_a_dag([
                {'temporary_id': 'ok1', 'dependency_ids': []},
                {'temporary_id': 'ok2', 'dependency_ids': ['ok1']},
                {'temporary_id': 'bad1', 'dependency_ids': ['bad2']},
                {'temporary_id': 'bad2', 'dependency_ids': ['bad1']},
            ])


class OverCapacityFlaggingTests(TwoCompanyTestCase):
    """A suggestion that would put somebody over a workload limit is
    **flagged**, not dropped -- §4."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            title='X', company=self.company_a, created_by=self.owner_a,
            deadline=timezone.now() + timedelta(days=90),
        )
        self.generation = AIGeneration.objects.create(
            project=self.project, requested_by=self.owner_a,
            requested_assignee_ids=[str(self.member_a.id)],
        )
        from ai_agent.context import build_assignee_refs
        self.generation.assignee_refs = build_assignee_refs([self.member_a.id])
        self.generation.save(update_fields=['assignee_refs'])

    def make_plan(self, **overrides):
        plan = {'success': True, 'data': {
            'provider': 'gemini', 'model': 'gemini-2.0-flash', 'summary': 'A plan',
            'tasks': [{
                'temporary_id': 't1', 'sequence': 1, 'title': 'Do it',
                'suggested_assignee_ref': 'member_1', 'suggested_assignee_rationale': 'Best fit',
            }],
        }}
        plan['data'].update(overrides)
        return plan

    def test_a_suggestion_within_capacity_is_kept_and_not_flagged(self):
        with patch('ai_agent.tasks.requests.post', return_value=make_response(200, self.make_plan())):
            process_ai_generation(str(self.generation.id))
        draft = AIGeneratedTask.objects.get(generation=self.generation)
        self.assertEqual(draft.suggested_assignee_id, self.member_a.id)
        self.assertEqual(draft.suggested_assignee_rationale, 'Best fit')
        self.assertFalse(draft.suggested_assignee_over_capacity)

    def test_a_suggestion_over_a_block_limit_is_flagged_and_kept(self):
        AssignmentPolicy.objects.create(
            company=self.company_a, enabled=True, max_active_tasks=0, enforcement='block',
        )
        with patch('ai_agent.tasks.requests.post', return_value=make_response(200, self.make_plan())):
            process_ai_generation(str(self.generation.id))
        draft = AIGeneratedTask.objects.get(generation=self.generation)
        # Kept, not dropped -- the reviewer decides, the pipeline does not.
        self.assertEqual(draft.suggested_assignee_id, self.member_a.id)
        self.assertTrue(draft.suggested_assignee_over_capacity)

    def test_a_disabled_policy_never_flags(self):
        AssignmentPolicy.objects.create(
            company=self.company_a, enabled=False, max_active_tasks=0, enforcement='block',
        )
        with patch('ai_agent.tasks.requests.post', return_value=make_response(200, self.make_plan())):
            process_ai_generation(str(self.generation.id))
        draft = AIGeneratedTask.objects.get(generation=self.generation)
        self.assertFalse(draft.suggested_assignee_over_capacity)

    def test_an_unresolvable_ref_drops_the_suggestion_and_its_rationale(self):
        plan = self.make_plan()
        plan['data']['tasks'][0]['suggested_assignee_ref'] = 'member_99'
        with patch('ai_agent.tasks.requests.post', return_value=make_response(200, plan)):
            process_ai_generation(str(self.generation.id))
        draft = AIGeneratedTask.objects.get(generation=self.generation)
        self.assertIsNone(draft.suggested_assignee_id)
        self.assertEqual(draft.suggested_assignee_rationale, '')
        self.assertFalse(draft.suggested_assignee_over_capacity)


class RequestProjectPlanIdempotencyTests(TwoCompanyTestCase):
    """§4: generalise the to-do generation's idempotency-key and in-flight
    lock pattern to every AI operation."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            title='X', company=self.company_a, created_by=self.owner_a,
            deadline=timezone.now() + timedelta(days=90),
        )

    def request(self, **kwargs):
        return async_to_sync(services.request_project_plan)(
            self.owner_a, self.project, prompt='Build it', **kwargs,
        )

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_a_repeated_idempotency_key_returns_the_same_generation(self, mock_delay):
        first, error = self.request(idempotency_key='key-1')
        self.assertIsNone(error)
        second, error = self.request(idempotency_key='key-1')
        self.assertIsNone(error)
        self.assertEqual(first.id, second.id)
        self.assertEqual(AIGeneration.objects.filter(project=self.project).count(), 1)
        mock_delay.assert_called_once()

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_different_keys_produce_different_generations(self, mock_delay):
        first, _ = self.request(idempotency_key='key-1')
        # Otherwise the second call is caught by the *in-flight* guard --
        # correctly, since it fires regardless of key -- rather than by the
        # idempotency-key path this test means to isolate.
        first.status = AIGeneration.STATUS.COMPLETED
        first.save(update_fields=['status'])
        second, _ = self.request(idempotency_key='key-2')
        self.assertNotEqual(first.id, second.id)

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_an_in_flight_generation_is_returned_rather_than_starting_a_second_one(self, mock_delay):
        first, _ = self.request()  # no key -- still caught by the in-flight guard
        self.assertEqual(first.status, AIGeneration.STATUS.PENDING)
        second, error = self.request()
        self.assertIsNone(error)
        self.assertEqual(first.id, second.id)
        self.assertEqual(AIGeneration.objects.filter(project=self.project).count(), 1)
        mock_delay.assert_called_once()

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_a_completed_generation_does_not_block_a_new_request(self, mock_delay):
        first, _ = self.request()
        first.status = AIGeneration.STATUS.COMPLETED
        first.save(update_fields=['status'])
        second, _ = self.request()
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(mock_delay.call_count, 2)

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_the_key_is_scoped_to_the_project(self, mock_delay):
        """The same key on a different project is not a repeat -- an
        idempotency key that leaked across tenants would be a much worse bug
        than the one it is meant to prevent."""
        other_project = Project.objects.create(
            title='Y', company=self.company_a, created_by=self.owner_a,
            deadline=timezone.now() + timedelta(days=90),
        )
        first, _ = self.request(idempotency_key='shared-key')
        second, _ = async_to_sync(services.request_project_plan)(
            self.owner_a, other_project, prompt='Build it', idempotency_key='shared-key',
        )
        self.assertNotEqual(first.id, second.id)

    def test_race_on_the_same_key_is_resolved_by_the_database_constraint(self):
        """Simulates the window between the app-level check and the insert:
        the DB constraint is the real guarantee, this is its behaviour under
        contention."""
        from django.db import IntegrityError

        AIGeneration.objects.create(
            project=self.project, requested_by=self.owner_a, idempotency_key='race-key',
        )
        with self.assertRaises(IntegrityError):
            AIGeneration.objects.create(
                project=self.project, requested_by=self.owner_a, idempotency_key='race-key',
            )

    def test_generations_with_no_key_never_collide(self):
        """Most rows carry no key at all -- the constraint is partial and
        must not force them to collide with each other."""
        first = AIGeneration.objects.create(project=self.project, requested_by=self.owner_a, idempotency_key='')
        second = AIGeneration.objects.create(project=self.project, requested_by=self.owner_a, idempotency_key='')
        self.assertNotEqual(first.id, second.id)


class RequestPlanEndpointIdempotencyTests(TwoCompanyTestCase):
    """The same behaviour, through the HTTP boundary."""

    def setUp(self):
        super().setUp()
        self.project = self.create_project(owner=self.owner_a)

    def request_plan(self, actor=None, **body):
        import json
        payload = {'prompt': 'Build it'}
        payload.update(body)
        return self.client.post(
            f"/api/v1/projects/{self.project['id']}/ai-plan/", json.dumps(payload),
            content_type='application/json', **auth_header(actor or self.owner_a),
        )

    @patch('ai_agent.services.process_ai_generation.delay')
    def test_a_repeated_idempotency_key_returns_202_with_the_same_generation(self, mock_delay):
        first = self.request_plan(idempotency_key='client-key-1')
        self.assertEqual(first.status_code, 202)
        second = self.request_plan(idempotency_key='client-key-1')
        self.assertEqual(second.status_code, 202)
        self.assertEqual(
            first.json()['data']['generation']['id'], second.json()['data']['generation']['id'],
        )
        mock_delay.assert_called_once()
