"""Characterization of the project/task authorization surface as it stands today.

This suite records **what the code does**, not what it should do. It is the
safety net for the access-model consolidation: extract
``resolve_project_access``, run this suite, and a green result is the only
proof the extraction was faithful. Behaviour believed to be wrong is recorded
here as-is and marked ``KNOWN DEFECT`` with the decision that will change it --
those expectations get changed one at a time, deliberately, with the reasoning
in the commit.

Two parts:

1. A generated matrix of 1440 rows over ``(actor) x (visibility) x (project
   department) x (ProjectMembership role) x (created_by / current_owner /
   neither) x (assigned a live task)``, evaluated against the real predicates
   in :mod:`projects_and_tasks.services` and compared against the checked-in
   baseline ``access_matrix_baseline.txt``. Regenerate it deliberately with
   ``WORKROOM_UPDATE_ACCESS_BASELINE=1 pytest ...`` and treat the resulting
   diff as the review artifact for the change.

2. Named tests for the gates that are not standalone predicates -- the
   visibility/department locks inlined in ``update_project`` -- and for the
   endpoint-level consequences of those predicates, notably that project
   *view* is the only thing standing between a user and task creation.

The matrix covers predicates rather than endpoints on purpose: an endpoint
layers ordering, validation and write behaviour on top, which belongs in the
named tests below and in each feature's own suite.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

from api.tests import auth_header
from asgiref.sync import async_to_sync
from company.models import Company, Sector
from departments_and_teams.models import Department
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from users.models import CompanyUserProfile, User

from projects_and_tasks import services
from projects_and_tasks.access import AccessLevel, resolve_project_access
from projects_and_tasks.models import Project, ProjectMembership, Task

BASELINE_PATH = Path(__file__).with_name('access_matrix_baseline.txt')
UPDATE_ENV = 'WORKROOM_UPDATE_ACCESS_BASELINE'
PASSWORD = 'Kx9#mQ2vLp8Z'

# Every column is one real predicate call, named after the function it comes
# from, so a reviewer can map a changed column straight back to a changed
# function rather than to a reimplementation of it living in this file.
COLUMNS = (
    ('view', 'user_can_view_project'),
    ('manage_proj', 'user_can_manage_project'),
    ('manage_task', 'user_can_manage_task, task created by someone else'),
    ('own_task', 'user_can_manage_task, task created by the actor'),
    ('set_status', 'user_can_update_task_status'),
    ('log_time', 'user_can_log_time'),
    ('approve', 'user_can_approve_task, task created by someone else'),
    ('deadline', 'user_can_extend_deadline'),
)

# The membership dimension: no row, or one at each of the three roles.
MEMBERSHIP_STATES = (
    ('none', None),
    ('viewer', ProjectMembership.Role.VIEWER),
    ('contrib', ProjectMembership.Role.CONTRIBUTOR),
    ('manager', ProjectMembership.Role.MANAGER),
)

VISIBILITIES = (
    Project.VISIBILITY.PRIVATE,
    Project.VISIBILITY.DEPARTMENT,
    Project.VISIBILITY.COMPANY,
    Project.VISIBILITY.PUBLIC,
)


class AccessWorldMixin:
    """One company, one actor per role, two departments, two projects."""

    @classmethod
    def build_world(cls):
        sector = Sector.objects.create(name='Software')

        # The company owner deliberately has no CompanyUserProfile row: that
        # is the legacy shape the codebase still special-cases in several
        # places, and the matrix has to record how it behaves before any
        # backfill changes it.
        cls.owner = User.objects.create_user(email='owner@example.com', username='owner', password=PASSWORD)
        cls.company = Company.objects.create(name='Company A', owner=cls.owner, sector=sector)
        cls.dept_match = Department.objects.create(name='Engineering', company=cls.company)
        cls.dept_other = Department.objects.create(name='Marketing', company=cls.company)

        cls.cm = cls._member('cm', CompanyUserProfile.Role.COMPANY_MANAGER, cls.dept_match)
        cls.dl = cls._member('dl', CompanyUserProfile.Role.DEPARTMENT_LEADER, cls.dept_match)
        cls.dm = cls._member('dm', CompanyUserProfile.Role.DEPARTMENT_MEMBER, cls.dept_match)
        # Stands in for "somebody else in the company" wherever a row needs a
        # created_by/current_owner/assignee who is not the actor.
        cls.other = cls._member('other', CompanyUserProfile.Role.DEPARTMENT_MEMBER, cls.dept_match)

        # A member of a different company entirely -- the only actor here who
        # should never reach anything, whatever the flags say.
        cls.stranger = User.objects.create_user(
            email='stranger@example.com', username='stranger', password=PASSWORD,
        )
        Company.objects.create(name='Company B', owner=cls.stranger, sector=sector)

    @classmethod
    def _member(cls, name, role, department):
        user = User.objects.create_user(email=f'{name}@example.com', username=name, password=PASSWORD)
        CompanyUserProfile.objects.create(user=user, company=cls.company, department=department, role=role)
        return user


class AccessMatrixCharacterizationTests(AccessWorldMixin, TestCase):
    """Generates the full access matrix and diffs it against the baseline."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()
        cls.actors = (
            ('Owner', cls.owner),
            ('CM', cls.cm),
            ('DL', cls.dl),
            ('DM', cls.dm),
            ('outsider', cls.stranger),
        )

        # Grants that live in the database -- a ProjectMembership row and a
        # task assignment -- cannot be simulated by mutating an instance, so
        # there is one persisted project per (membership role, has an assigned
        # task) combination and the row picks between them. Everything the
        # resolver reads off the instance (visibility, department, created_by,
        # current_owner) is still applied in memory and never saved, which
        # keeps the matrix honest about what it is measuring.
        now = timezone.now()
        common = {
            'company': cls.company, 'start_date': now, 'deadline': now + timedelta(days=365),
            'created_by': cls.other, 'current_owner': cls.other,
        }
        everyone = [cls.owner, cls.cm, cls.dl, cls.dm, cls.stranger]
        cls.projects = {}
        for membership_name, membership_role in MEMBERSHIP_STATES:
            for has_task in (False, True):
                project = Project.objects.create(
                    title=f'member={membership_name} task={has_task}', **common,
                )
                if membership_role is not None:
                    ProjectMembership.objects.bulk_create([
                        ProjectMembership(project=project, user=person, role=membership_role)
                        for person in everyone
                    ])
                if has_task:
                    Task.objects.bulk_create([
                        Task(
                            project=project, title=f'for {person.username}', created_by=cls.other,
                            assigned_to=person, deadline=common['deadline'],
                        )
                        for person in everyone
                    ])
                cls.projects[(membership_name, has_task)] = project

    def _evaluate(self, actor, project, task):
        """One row's worth of predicate answers, in COLUMNS order."""
        call = async_to_sync
        own_task = Task(project=project, created_by_id=actor.id, assigned_to_id=task.assigned_to_id)
        return (
            call(services.user_can_view_project)(actor, project),
            call(services.user_can_manage_project)(actor, project),
            call(services.user_can_manage_task)(actor, task),
            call(services.user_can_manage_task)(actor, own_task),
            call(services.user_can_update_task_status)(actor, task),
            call(services.user_can_log_time)(actor, task),
            call(services.user_can_approve_task)(actor, task),
            call(services.user_can_extend_deadline)(actor, project),
        )

    def _generate(self):
        departments = (('match', self.dept_match.id), ('other', self.dept_other.id), ('none', None))
        widths = [max(len(name), 3) for name, _ in COLUMNS]
        header = 'actor    | vis        | dept  | member  | rel     | asg | ' + ' '.join(
            name.ljust(width) for (name, _), width in zip(COLUMNS, widths)
        )
        lines = [
            '# Generated by projects_and_tasks/test_access_matrix.py -- do not hand-edit.',
            '# Regenerate with WORKROOM_UPDATE_ACCESS_BASELINE=1 and review the diff.',
            '#',
            '# Columns:',
            *[f'#   {name:<12} {source}' for name, source in COLUMNS],
            '#',
            '# Dimensions:',
            '#   member  the actor ProjectMembership role on this project, or none',
            '#   rel     none | creator (actor is created_by) | owner (actor is current_owner)',
            '#           "both" is omitted: effective access is a max, so it adds nothing',
            '#   asg     the actor is assigned a live task on this project',
            '',
            header,
            '-' * len(header),
        ]
        for actor_name, actor in self.actors:
            for visibility in VISIBILITIES:
                for dept_name, dept_id in departments:
                    for membership_name, _ in MEMBERSHIP_STATES:
                        for is_assignee in (False, True):
                            project = self.projects[(membership_name, is_assignee)]
                            project.visibility = visibility
                            project.department_id = dept_id
                            for relation in ('none', 'creator', 'owner'):
                                project.created_by_id = actor.id if relation == 'creator' else self.other.id
                                project.current_owner_id = actor.id if relation == 'owner' else self.other.id
                                task = Task(
                                    project=project, created_by_id=self.other.id,
                                    assigned_to_id=actor.id if is_assignee else self.other.id,
                                )
                                answers = ' '.join(
                                    ('1' if value else '0').ljust(width)
                                    for value, width in zip(self._evaluate(actor, project, task), widths)
                                )
                                lines.append(
                                    f'{actor_name:<8} | {visibility:<10} | {dept_name:<5} | '
                                    f'{membership_name:<7} | {relation:<7} | {"1" if is_assignee else "0"}   | '
                                    f'{answers}'.rstrip()
                                )
        return '\n'.join(lines) + '\n'

    def test_access_matrix_matches_baseline(self):
        generated = self._generate()
        if os.environ.get(UPDATE_ENV):
            # newline='\n' explicitly: a baseline regenerated on Windows would
            # otherwise land in CRLF and diff against every line of the same
            # file recorded anywhere else, which would make the one artifact a
            # reviewer is meant to read useless.
            BASELINE_PATH.write_text(generated, encoding='utf-8', newline='\n')
            self.skipTest(f'{UPDATE_ENV} set: baseline rewritten, review the diff before committing it')
        self.assertTrue(
            BASELINE_PATH.exists(), f'Missing {BASELINE_PATH.name}; generate it with {UPDATE_ENV}=1',
        )
        expected = BASELINE_PATH.read_text(encoding='utf-8')
        if generated == expected:
            return
        expected_rows = expected.splitlines()
        generated_rows = generated.splitlines()
        changed = [
            f'  - {old}\n  + {new}' for old, new in zip(expected_rows, generated_rows) if old != new
        ]
        self.fail(
            'Project access behaviour changed against the recorded baseline. If the change is '
            'intended, re-record it deliberately and account for every row in the commit body.\n'
            f'{len(changed)} row(s) differ, {len(generated_rows) - len(expected_rows):+d} row(s) '
            'added:\n' + '\n'.join(changed[:40])
        )


class BaselineInvariantTests(SimpleTestCase):
    """Properties that must hold across the whole recorded matrix, asserted
    against the committed baseline rather than by re-deriving it.

    These need no database: the baseline is the recorded truth, and stating
    the invariants separately means a regression fails with "MANAGE no longer
    implies VIEW" rather than with a 900-line diff a reviewer has to decode.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rows = []
        for line in BASELINE_PATH.read_text(encoding='utf-8').splitlines():
            if line.startswith('#') or line.startswith('-') or '|' not in line or 'actor' in line:
                continue
            actor, visibility, department, member, relation, assignee, answers = (
                part.strip() for part in line.split('|')
            )
            flags = f'member={member} rel={relation} asg={assignee}'
            values = dict(zip([name for name, _ in COLUMNS], [v == '1' for v in answers.split()]))
            cls.rows.append((actor, visibility, department, flags, values))

    def test_the_baseline_covers_the_whole_cross_product(self):
        actors, visibilities, departments, memberships, assignee, relations = 5, 4, 3, 4, 2, 3
        self.assertEqual(
            len(self.rows), actors * visibilities * departments * memberships * assignee * relations,
        )

    def test_manage_always_implies_view(self):
        """The property an ordered AccessLevel exists to guarantee, and the
        reason the resolver had to be extracted rather than left as scattered
        predicates: 30 combinations previously granted management of a project
        the same user could not open."""
        for actor, visibility, department, flags, values in self.rows:
            if values['manage_proj']:
                self.assertTrue(
                    values['view'],
                    f'{actor} / {visibility} / dept={department} / {flags} can manage but not view',
                )

    def test_a_non_member_is_granted_nothing_beyond_reading_a_public_project(self):
        """Tenant isolation stated as a property over every row rather than as
        one example. ``public`` is the single deliberate exception."""
        for actor, visibility, department, flags, values in self.rows:
            if actor != 'outsider':
                continue
            where = f'{actor} / {visibility} / dept={department} / {flags}'
            self.assertEqual(values['view'], visibility == 'public', f'{where}: unexpected view')
            for capability in ('manage_proj', 'manage_task', 'own_task', 'approve', 'deadline'):
                self.assertFalse(values[capability], f'{where}: unexpected {capability}')

    def test_the_company_owner_can_always_manage(self):
        """No combination of visibility, department or relationship takes a
        project away from the person who owns the company."""
        for actor, visibility, department, flags, values in self.rows:
            if actor == 'Owner':
                self.assertTrue(values['manage_proj'], f'Owner blocked on {visibility}/{department}/{flags}')


class KnownDefectCharacterizationTests(AccessWorldMixin, TestCase):
    """The behaviours the access-model work is going to change, pinned one test
    each so that changing one is a visible, single-line diff rather than a
    silent shift inside the generated matrix."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()

    def setUp(self):
        now = timezone.now()
        self.project = Project.objects.create(
            title='Website Revamp', company=self.company, department=self.dept_match,
            visibility=Project.VISIBILITY.COMPANY, start_date=now, deadline=now + timedelta(days=365),
            created_by=self.other, current_owner=self.other,
        )

    def create_task_via_api(self, actor, **overrides):
        body = {
            'title': 'Write the brief',
            'description': 'Draft it',
            'priority': 'medium',
            'deadline': (self.project.deadline - timedelta(days=1)).isoformat(),
        }
        body.update(overrides)
        return self.client.post(
            f'/api/v1/projects/{self.project.id}/tasks/', json.dumps(body),
            content_type='application/json', **auth_header(actor),
        )

    def test_project_view_is_all_it_takes_to_create_a_task(self):
        """KNOWN DEFECT (decision sec.2): task creation is gated on
        user_can_view_project, so company-wide visibility -- which is meant to
        be discovery only -- hands every member of the company the ability to
        add work to any project. Task creation moves behind project MANAGE."""
        response = self.create_task_via_api(self.dm)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Task.objects.filter(project=self.project, created_by=self.dm).exists())

    def test_task_creator_keeps_managing_a_task_they_cannot_otherwise_touch(self):
        """KNOWN DEFECT (decision sec.2): user_can_manage_task short-circuits on
        task.created_by, so a DM who created a task keeps full edit/assign/
        archive rights over it regardless of their access to the project."""
        task = Task.objects.create(
            project=self.project, title='Theirs', created_by=self.dm, deadline=self.project.deadline,
        )
        self.assertFalse(async_to_sync(services.user_can_manage_project)(self.dm, self.project))
        self.assertTrue(async_to_sync(services.user_can_manage_task)(self.dm, task))

    def test_moving_a_deadline_belongs_to_whoever_manages_the_project(self):
        """FIXED (was D3): deadline changes were created_by-only, which locked
        out the current owner, the company Owner, a CM and the department's own
        leader -- and meant a creator who left the company froze the project's
        dates permanently. They now follow MANAGE."""
        self.project.current_owner = self.dm
        self.project.save(update_fields=['current_owner'])
        for actor in (self.owner, self.cm, self.dl, self.dm):
            updated, error = async_to_sync(services.change_project_deadline)(
                actor, self.project, self.project.deadline + timedelta(days=1), reason='Scope changed',
            )
            self.assertIsNone(error, f'{actor.email} was refused')
            self.assertIsNotNone(updated)

    def test_moving_a_deadline_requires_a_reason(self):
        """The counterweight to widening who may do it. A date moving under the
        people doing the work is what makes a deadline feel arbitrary."""
        for reason in ('', '   ', None):
            _, error = async_to_sync(services.change_project_deadline)(
                self.owner, self.project, self.project.deadline + timedelta(days=1), reason=reason,
            )
            self.assertEqual(error, 'reason_required', f'blank reason {reason!r} was accepted')

    def test_a_deadline_can_now_be_pulled_in(self):
        """FIXED (was D4): deadlines could only ever move outwards, so the most
        common real correction had no route through the product."""
        earlier = self.project.deadline - timedelta(days=1)
        updated, error = async_to_sync(services.change_project_deadline)(
            self.owner, self.project, earlier, reason='Client pulled the date in',
        )
        self.assertIsNone(error)
        self.assertEqual(updated.deadline, earlier)

    def test_shortening_is_blocked_by_the_tasks_it_would_strand(self):
        """Blocked by the invariant, not by direction -- and the caller gets the
        offending tasks back rather than a bare refusal."""
        task = Task.objects.create(
            project=self.project, title='Runs to the end', created_by=self.other,
            deadline=self.project.deadline,
        )
        result, error = async_to_sync(services.change_project_deadline)(
            self.owner, self.project, self.project.deadline - timedelta(days=1), reason='Pull in',
        )
        self.assertEqual(error, 'tasks_exceed_deadline')
        self.assertEqual([t.id for t in result], [task.id])

    def test_a_task_deadline_may_equal_the_project_deadline(self):
        """FIXED (was D5): the strict inequality is what forced the AI
        generator to subtract an hour from every task it produced."""
        task, error = async_to_sync(services.create_task)(
            self.other, self.project, title='On the boundary', description='x',
            priority='medium', deadline=self.project.deadline,
        )
        self.assertIsNone(error)
        self.assertEqual(task.deadline, self.project.deadline)

    def test_a_task_deadline_still_cannot_fall_after_the_project_deadline(self):
        _, error = async_to_sync(services.create_task)(
            self.other, self.project, title='One second too far', description='x',
            priority='medium', deadline=self.project.deadline + timedelta(seconds=1),
        )
        self.assertEqual(error, 'invalid_deadline')

    def test_a_task_deadline_defaults_to_the_project_deadline(self):
        """A task that runs to the end of its project is the common case, and
        making people retype the project's own date to say so was friction."""
        task, error = async_to_sync(services.create_task)(
            self.other, self.project, title='No deadline given', description='x',
            priority='medium', deadline=None,
        )
        self.assertIsNone(error)
        self.assertEqual(task.deadline, self.project.deadline)

    def test_a_late_task_can_still_be_submitted_and_is_flagged(self):
        """FIXED (was D6): refusing a late submission meant a task that ran a
        day over could never reach Done by any route -- a dead end rather than
        a guardrail, and one that pushed people into backdating deadlines to
        close out real work."""
        task = Task.objects.create(
            project=self.project, title='Late', created_by=self.other, assigned_to=self.dm,
            status=Task.STATUS.IN_PROGRESS, deadline=timezone.now() - timedelta(hours=3),
        )
        approval, error = async_to_sync(services.submit_task_for_approval)(
            self.dm, task, links=['https://example.com/evidence'],
        )
        self.assertIsNone(error)
        self.assertTrue(approval.submitted_late)
        self.assertGreater(approval.late_by, timedelta(hours=2))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.STATUS.IN_REVIEW)

    def test_an_on_time_submission_is_not_flagged(self):
        task = Task.objects.create(
            project=self.project, title='On time', created_by=self.other, assigned_to=self.dm,
            status=Task.STATUS.IN_PROGRESS, deadline=timezone.now() + timedelta(days=1),
        )
        approval, error = async_to_sync(services.submit_task_for_approval)(
            self.dm, task, links=['https://example.com/evidence'],
        )
        self.assertIsNone(error)
        self.assertFalse(approval.submitted_late)
        self.assertIsNone(approval.late_by)

    def test_every_other_submission_guard_still_refuses(self):
        """Allowing late submission removed one guard and must not have
        loosened any of the others."""
        task = Task.objects.create(
            project=self.project, title='Guarded', created_by=self.other, assigned_to=self.dm,
            status=Task.STATUS.IN_PROGRESS, deadline=timezone.now() - timedelta(hours=1),
        )
        _, error = async_to_sync(services.submit_task_for_approval)(
            self.other, task, links=['https://example.com/x'],
        )
        self.assertEqual(error, 'forbidden', 'a non-assignee was allowed to submit')

        _, error = async_to_sync(services.submit_task_for_approval)(self.dm, task)
        self.assertEqual(error, 'no_evidence')

        async_to_sync(services.submit_task_for_approval)(self.dm, task, links=['https://example.com/x'])
        # Re-fetched rather than refresh_from_db()'d: refreshing clears cached
        # relations, and the async service layer expects its task to arrive
        # with project__company already selected, exactly as the router
        # supplies it. Traversing the FK inside the event loop is a
        # SynchronousOnlyOperation, not a slow query.
        task = Task.objects.select_related('project', 'project__company').get(id=task.id)
        _, error = async_to_sync(services.submit_task_for_approval)(self.dm, task, links=['https://example.com/y'])
        self.assertEqual(error, 'invalid_status', 'a task already In Review was submitted again')

    def test_a_public_project_is_readable_by_a_member_of_another_company(self):
        """KNOWN DEFECT (decision sec.1): ``public`` short-circuits the company
        check entirely, so any authenticated user of any tenant can read the
        project by id -- and a Department Member can set that visibility. The
        transition moves behind Owner/CM plus a company flag."""
        self.project.visibility = Project.VISIBILITY.PUBLIC
        self.assertTrue(async_to_sync(services.user_can_view_project)(self.stranger, self.project))

    def test_a_department_member_can_publish_a_project_they_manage(self):
        """KNOWN DEFECT (decision sec.1): the only thing stopping a DM here is
        update_project's visibility lock; a DM who owns the project through any
        other route is not blocked from ``public`` by anything else."""
        self.assertTrue(async_to_sync(services.user_can_manage_project)(self.dl, self.project))
        updated, error = async_to_sync(services.update_project)(
            self.dl, self.project, {'visibility': Project.VISIBILITY.PUBLIC},
        )
        self.assertIsNone(error)
        self.assertEqual(updated.visibility, Project.VISIBILITY.PUBLIC)

    def grant(self, user, role):
        return ProjectMembership.objects.create(project=self.project, user=user, role=role)

    def test_a_department_member_cannot_change_visibility_even_when_they_manage(self):
        """The DM visibility lock lives inline in update_project rather than in
        a predicate, so it is invisible to the generated matrix.

        The membership is what makes this test meaningful now. It used to reach
        the lock by making the DM ``created_by``, which granted MANAGE; that
        route is gone, so without an explicit manager grant the DM is refused
        earlier with ``forbidden`` and the lock itself is never exercised."""
        self.grant(self.dm, ProjectMembership.Role.MANAGER)
        _, error = async_to_sync(services.update_project)(
            self.dm, self.project, {'visibility': Project.VISIBILITY.PRIVATE},
        )
        self.assertEqual(error, 'visibility_locked')

    def test_a_department_scoped_role_cannot_move_a_project_between_departments(self):
        """Another inline gate in update_project, recorded so the consolidation
        does not quietly drop it. Both actors are given a manager membership so
        the department lock is what refuses them, not a lack of access."""
        for actor in (self.dl, self.dm):
            ProjectMembership.objects.update_or_create(
                project=self.project, user=actor, defaults={'role': ProjectMembership.Role.MANAGER},
            )
            _, error = async_to_sync(services.update_project)(
                actor, self.project, {'department_id': self.dept_other.id},
            )
            self.assertEqual(error, 'department_locked', f'{actor.email} was allowed to move the project')

    def test_creating_a_project_no_longer_grants_management_of_it(self):
        """created_by is permanent, immutable provenance and grants VIEW only.
        It records who started the project; it is not a claim on it. Anyone who
        needs to keep managing what they created holds a manager membership --
        which, unlike created_by, can be revoked."""
        self.project.created_by = self.dm
        self.project.save(update_fields=['created_by'])
        self.assertEqual(async_to_sync(resolve_project_access)(self.dm, self.project), AccessLevel.VIEW)
        self.assertFalse(async_to_sync(services.user_can_manage_project)(self.dm, self.project))

        self.grant(self.dm, ProjectMembership.Role.MANAGER)
        self.assertEqual(async_to_sync(resolve_project_access)(self.dm, self.project), AccessLevel.MANAGE)

    def test_being_assigned_a_task_grants_contribute_but_never_manage(self):
        """An assignment is itself a grant: somebody with MANAGE decided this
        person should do this work, and they cannot do it without reaching the
        project. It stops at CONTRIBUTE -- doing the work is not running it."""
        self.project.visibility = Project.VISIBILITY.PRIVATE
        self.project.save(update_fields=['visibility'])
        self.assertIsNone(async_to_sync(resolve_project_access)(self.dm, self.project))

        task = Task.objects.create(
            project=self.project, title='Do it', created_by=self.other, assigned_to=self.dm,
            deadline=self.project.deadline,
        )
        self.assertEqual(async_to_sync(resolve_project_access)(self.dm, self.project), AccessLevel.CONTRIBUTE)

        task.is_deleted = True
        task.save(update_fields=['is_deleted'])
        self.assertIsNone(async_to_sync(resolve_project_access)(self.dm, self.project))

    def test_visibility_grants_discovery_and_never_capability(self):
        """The rule the whole model turns on. Widening visibility must let more
        people find a project and never let more people change it."""
        self.project.visibility = Project.VISIBILITY.COMPANY
        self.project.save(update_fields=['visibility'])
        self.assertEqual(async_to_sync(resolve_project_access)(self.dm, self.project), AccessLevel.VIEW)
        self.assertFalse(async_to_sync(services.user_can_manage_project)(self.dm, self.project))

    def test_a_per_project_reference_grants_nothing_to_a_non_member(self):
        """FIXED (was D10): every predicate resolves company membership before
        consulting ``created_by``, ``current_owner`` or ``assigned_to``.

        Nothing in the API sets a cross-tenant reference today -- assignees,
        collaborators and new owners are all validated against the company --
        so this was a missing backstop rather than a live breach. It stops
        being theoretical once a reference can outlive the membership behind
        it, which is exactly what ``created_by`` already does: SET_NULL on user
        deletion, but left untouched when someone is merely removed from the
        company. See the two tests below."""
        outsider_task = Task(project=self.project, created_by_id=self.stranger.id)
        self.assertFalse(async_to_sync(services.user_can_manage_task)(self.stranger, outsider_task))

        self.project.current_owner = self.stranger
        self.project.created_by = self.stranger
        self.assertFalse(async_to_sync(services.user_can_manage_project)(self.stranger, self.project))
        self.assertFalse(async_to_sync(services.user_can_view_project)(self.stranger, self.project))
        self.assertFalse(async_to_sync(services.user_can_extend_deadline)(self.stranger, self.project))

    def test_a_removed_member_loses_access_to_the_projects_they_created(self):
        """The practical case the membership check exists for: removal deletes
        the CompanyUserProfile but deliberately leaves ``created_by`` intact as
        immutable provenance -- so provenance must not double as a grant."""
        self.project.created_by = self.dm
        self.project.visibility = Project.VISIBILITY.PRIVATE
        self.project.save(update_fields=['created_by', 'visibility'])
        self.assertTrue(async_to_sync(services.user_can_view_project)(self.dm, self.project))

        CompanyUserProfile.objects.filter(user=self.dm, company=self.company).delete()
        self.assertFalse(async_to_sync(services.user_can_view_project)(self.dm, self.project))
        self.assertFalse(async_to_sync(services.user_can_manage_project)(self.dm, self.project))

    def test_a_deactivated_member_loses_access_the_same_way(self):
        """Deactivation revokes company access without touching Django auth
        (PRESERVE). The JWT still authenticates; every company-scoped answer
        goes to no."""
        self.project.created_by = self.dm
        self.project.save(update_fields=['created_by'])
        CompanyUserProfile.objects.filter(user=self.dm, company=self.company).update(is_active=False)
        self.assertFalse(async_to_sync(services.user_can_view_project)(self.dm, self.project))
        self.assertFalse(async_to_sync(services.user_can_manage_project)(self.dm, self.project))

    def test_a_public_project_is_still_readable_across_tenants(self):
        """Unchanged on purpose. ``public`` is meant to place a project outside
        the tenant boundary, so it is checked before membership. Restricting
        who may *set* it is a separate decision (sec.1), handled at the
        transition rather than here."""
        self.project.visibility = Project.VISIBILITY.PUBLIC
        self.assertTrue(async_to_sync(services.user_can_view_project)(self.stranger, self.project))
        self.assertFalse(async_to_sync(services.user_can_manage_project)(self.stranger, self.project))

    def test_a_department_leader_can_now_open_the_private_project_they_manage(self):
        """FIXED (was D11): a DL of the project's own department could edit,
        archive and transfer a private project that GET returned 403 for, and a
        DM who was current_owner was in the same position. Thirty combinations
        in all. The ordered AccessLevel cannot express "manage but not view",
        and granting read to someone who can already rewrite the thing is
        strictly the smaller of the two ways to resolve it."""
        self.project.visibility = Project.VISIBILITY.PRIVATE
        self.project.save(update_fields=['visibility'])

        self.assertEqual(async_to_sync(resolve_project_access)(self.dl, self.project), AccessLevel.MANAGE)
        self.assertTrue(async_to_sync(services.user_can_view_project)(self.dl, self.project))

        self.project.current_owner = self.dm
        self.assertEqual(async_to_sync(resolve_project_access)(self.dm, self.project), AccessLevel.MANAGE)
        self.assertTrue(async_to_sync(services.user_can_view_project)(self.dm, self.project))

    def test_the_resolver_returns_the_maximum_of_every_grant(self):
        """Effective access is a max, not a first-match: adding a source can
        widen access for the people it names but never narrow it for anyone
        else. A DM who is merely a collaborator on a private project gets VIEW;
        the same DM as current_owner gets MANAGE and keeps it."""
        self.project.visibility = Project.VISIBILITY.PRIVATE
        self.project.save(update_fields=['visibility'])
        self.assertIsNone(async_to_sync(resolve_project_access)(self.dm, self.project))

        membership = self.grant(self.dm, ProjectMembership.Role.VIEWER)
        self.assertEqual(async_to_sync(resolve_project_access)(self.dm, self.project), AccessLevel.VIEW)

        # A lower-ranked grant never reduces a higher one that already applies.
        self.project.current_owner = self.dm
        self.assertEqual(async_to_sync(resolve_project_access)(self.dm, self.project), AccessLevel.MANAGE)

        membership.role = ProjectMembership.Role.CONTRIBUTOR
        membership.save(update_fields=['role'])
        self.assertEqual(async_to_sync(resolve_project_access)(self.dm, self.project), AccessLevel.MANAGE)

    def test_the_resolver_returns_none_for_someone_with_no_relationship(self):
        self.project.visibility = Project.VISIBILITY.PRIVATE
        self.project.save(update_fields=['visibility'])
        self.assertIsNone(async_to_sync(resolve_project_access)(self.dm, self.project))
        self.assertIsNone(async_to_sync(resolve_project_access)(self.stranger, self.project))

    def test_the_project_list_and_the_detail_check_disagree(self):
        """KNOWN DEFECT (decision sec.10): list_projects_for_user grants a
        creator visibility only on *private* projects, while
        user_can_view_project grants a creator visibility on any project. A DM
        who created a project in another department can open it by id but will
        never see it in their own list."""
        self.project.visibility = Project.VISIBILITY.DEPARTMENT
        self.project.department = self.dept_other
        self.project.created_by = self.dm
        self.project.save(update_fields=['visibility', 'department', 'created_by'])

        self.assertTrue(async_to_sync(services.user_can_view_project)(self.dm, self.project))
        listed = async_to_sync(services.list_projects_for_user)(self.dm)
        self.assertNotIn(self.project.id, [p.id for p in listed])
