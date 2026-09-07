"""Characterization of the project/task authorization surface as it stands today.

This suite records **what the code does**, not what it should do. It is the
safety net for the access-model consolidation: extract
``resolve_project_access``, run this suite, and a green result is the only
proof the extraction was faithful. Behaviour believed to be wrong is recorded
here as-is and marked ``KNOWN DEFECT`` with the decision that will change it --
those expectations get changed one at a time, deliberately, with the reasoning
in the commit.

Two parts:

1. A generated matrix over
   ``(actor) x (visibility) x (project department) x (created_by,
   current_owner, collaborator, assignee)``, evaluated against the real
   predicates in :mod:`projects_and_tasks.services` and compared against the
   checked-in baseline ``access_matrix_baseline.txt``. Regenerate it
   deliberately with ``WORKROOM_UPDATE_ACCESS_BASELINE=1 pytest ...`` and treat
   the resulting diff as the review artifact for the change.

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
from django.test import TestCase
from django.utils import timezone
from users.models import CompanyUserProfile, User

from projects_and_tasks import services
from projects_and_tasks.models import Project, Task

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

        # Two persisted projects differing only in their collaborator set, so
        # the collaborator dimension costs no per-row M2M writes. Every other
        # dimension is applied to these instances in memory: the predicates
        # under test only read attributes and the collaborators relation, and
        # never saving keeps the matrix honest about what it is measuring.
        now = timezone.now()
        common = {
            'company': cls.company, 'start_date': now, 'deadline': now + timedelta(days=365),
            'created_by': cls.other, 'current_owner': cls.other,
        }
        cls.project_plain = Project.objects.create(title='No collaborators', **common)
        cls.project_collab = Project.objects.create(title='All collaborators', **common)
        cls.project_collab.collaborators.set([cls.owner, cls.cm, cls.dl, cls.dm, cls.stranger])

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
        header = 'actor    | vis        | dept  | by own col asg | ' + ' '.join(
            name.ljust(width) for (name, _), width in zip(COLUMNS, widths)
        )
        lines = [
            '# Generated by projects_and_tasks/test_access_matrix.py -- do not hand-edit.',
            '# Regenerate with WORKROOM_UPDATE_ACCESS_BASELINE=1 and review the diff.',
            '#',
            '# Columns:',
            *[f'#   {name:<12} {source}' for name, source in COLUMNS],
            '#',
            '# Flags: by = actor is project.created_by, own = actor is project.current_owner,',
            '#        col = actor is a collaborator, asg = actor is the task assignee.',
            '',
            header,
            '-' * len(header),
        ]
        for actor_name, actor in self.actors:
            for visibility in VISIBILITIES:
                for dept_name, dept_id in departments:
                    for is_collaborator in (False, True):
                        project = self.project_collab if is_collaborator else self.project_plain
                        project.visibility = visibility
                        project.department_id = dept_id
                        for is_creator in (False, True):
                            project.created_by_id = actor.id if is_creator else self.other.id
                            for is_owner in (False, True):
                                project.current_owner_id = actor.id if is_owner else self.other.id
                                for is_assignee in (False, True):
                                    task = Task(
                                        project=project, created_by_id=self.other.id,
                                        assigned_to_id=actor.id if is_assignee else self.other.id,
                                    )
                                    flags = ' '.join(
                                        ' 1' if flag else ' 0'
                                        for flag in (is_creator, is_owner, is_collaborator, is_assignee)
                                    )
                                    answers = ' '.join(
                                        ('1' if value else '0').ljust(width)
                                        for value, width in zip(self._evaluate(actor, project, task), widths)
                                    )
                                    lines.append(
                                        f'{actor_name:<8} | {visibility:<10} | {dept_name:<5} |'
                                        f'{flags} | {answers}'.rstrip()
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

    def test_only_the_project_creator_can_move_a_deadline(self):
        """KNOWN DEFECT (decision sec.2): deadline changes are creator-only, so
        the current owner, the company Owner, a CM and the department's own
        leader are all locked out, and a creator who leaves the company freezes
        the deadline permanently. Replaced by "anyone who can MANAGE, with a
        required reason, audited"."""
        for actor in (self.owner, self.cm, self.dl, self.dm):
            self.assertFalse(
                async_to_sync(services.user_can_extend_deadline)(actor, self.project),
                f'{actor.email} unexpectedly allowed to change the deadline',
            )
        self.project.created_by = self.dm
        self.assertTrue(async_to_sync(services.user_can_extend_deadline)(self.dm, self.project))

    def test_deadline_changes_are_extend_only(self):
        """KNOWN DEFECT (decision sec.2): a deadline can only ever move
        forwards. Shortening becomes allowed, blocked only when it would break
        the task invariant."""
        self.project.created_by = self.dm
        self.project.save(update_fields=['created_by'])
        earlier = self.project.deadline - timedelta(days=1)
        _, error = async_to_sync(services.extend_project_deadline)(self.dm, self.project, earlier)
        self.assertEqual(error, 'not_an_extension')

    def test_a_task_deadline_must_fall_strictly_before_the_project_deadline(self):
        """KNOWN DEFECT (decision sec.2): the strict inequality is what forces
        the AI generator's one-hour buffer. Becomes ``<=``."""
        _, error = async_to_sync(services.create_task)(
            self.other, self.project, title='On the boundary', description='x',
            priority='medium', deadline=self.project.deadline,
        )
        self.assertEqual(error, 'invalid_deadline')

    def test_a_task_cannot_be_submitted_once_its_deadline_has_passed(self):
        """KNOWN DEFECT (decision sec.2): a task that goes one minute late can
        never reach Done. Late submission becomes allowed and flagged."""
        task = Task.objects.create(
            project=self.project, title='Late', created_by=self.other, assigned_to=self.dm,
            status=Task.STATUS.IN_PROGRESS, deadline=timezone.now() - timedelta(hours=1),
        )
        _, error = async_to_sync(services.submit_task_for_approval)(
            self.dm, task, links=['https://example.com/evidence'],
        )
        self.assertEqual(error, 'deadline_passed')

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

    def test_a_department_member_cannot_change_visibility_at_all(self):
        """Current behaviour, preserved for contrast with the test above: the
        DM lock lives inline in update_project rather than in a predicate, so
        it is invisible to the generated matrix."""
        self.project.created_by = self.dm
        self.project.save(update_fields=['created_by'])
        _, error = async_to_sync(services.update_project)(
            self.dm, self.project, {'visibility': Project.VISIBILITY.PRIVATE},
        )
        self.assertEqual(error, 'visibility_locked')

    def test_a_department_scoped_role_cannot_move_a_project_between_departments(self):
        """Current behaviour: another inline gate in update_project, recorded
        here so the consolidation does not quietly drop it."""
        for actor in (self.dl, self.dm):
            self.project.created_by = actor
            self.project.save(update_fields=['created_by'])
            _, error = async_to_sync(services.update_project)(
                actor, self.project, {'department_id': self.dept_other.id},
            )
            self.assertEqual(error, 'department_locked', f'{actor.email} was allowed to move the project')

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
