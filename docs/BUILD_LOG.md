# Build log — V2 foundation

Branch: `feat/v2-foundation`. One entry per work package: what changed, files
touched, judgment calls made, anything deferred.

---

## Work package order

Derived from the dependency order in the prompt rather than its section
numbering — the access spine (§10) has to exist before the features that use
it, and the audit log has to exist before the mutations that record through it.

| WP | Covers | Status |
|---|---|---|
| WP1 | Characterization suite + permission inventory (§10, prerequisite) | **done** |
| WP2 | `AuditEvent` + `record_event()` (§10) | **done** |
| WP3 | `resolve_project_access` extracted, behaviour-preserving (§10) | **done** |
| WP4 | ProjectMembership and the four access levels (§10) | **done** |
| WP5 | Deadlines: `<=`, AI buffer removal, MANAGE + reason + audit; late submission (§2) | **done** |
| WP6 | `ApprovalRequest`; visibility escalation and the public gate (§1, §10) | **done** |
| WP7a | Task field-level authority; `created_by` stops granting MANAGE (§2) | **done** |
| WP7b | Task creation behind MANAGE; task proposals; break-into-steps (§2) | **done** |
| WP8 | Task dependencies — `blocks` / `relates_to` (§2) | **done** |
| WP9 | `ProjectBrief` and brief-assisted creation (§1) | |
| WP10 | Skills, professions, capacity, workload, `AssignmentPolicy` (§3) | |
| WP11 | AI pipeline: allow-list serializer, re-validation, token accounting (§4) | |
| WP12 | AI assistant privacy; health-summary anonymity (§5) | |
| WP13 | Events: audience, attendees, RRULE (§6) | |
| WP14 | Unified `Document` model with scopes (§7) | **done** |
| WP15 | Analytics tiers (§8) | **done** |
| WP16 | To-dos: timezone, eligibility, supersede semantics (§9) | **done** |
| WP17 | Plans, `Entitlements`, `UsageCounter`, enforcement (§11) | **done** |
| WP18 | Integration seams (§12) | **done** |
| WP19 | `docs/DECISIONS.md` (§13) | **done** |
| WP20 | Frontend consolidation: `useProjectAccess`, regenerated types (§ throughout) | |
| WP21 | Owner-with-no-membership backfill, then delete every "owner might have no profile" branch (§10) | **done** |
| WP22 | Company context returns the membership, accepts an explicit company id (§10) | **done** |
| WP23 | `docs/MONETIZATION_PLAN.md` + README authorization/audit sections (DEFINITION OF DONE) | **partial** — plan written, README outstanding |

Three rows were added to this table on 2026-09-08, after a read-back against
the prompt. WP21 and WP22 are the two tail paragraphs of §10 that are not part
of the access resolver itself, and were missed when the table was first drawn
because §10 was filed as "the resolver". WP23 is the two DEFINITION OF DONE
artifacts that are not tied to any decision section. None of them were started;
they were simply not being tracked.

Note that WP21 will move the access-matrix baseline: the matrix world gives the
company owner no `CompanyUserProfile`, which is exactly the legacy shape the
backfill removes (see WP1, judgment call 4).

---

## WP1 — Characterization suite and permission inventory

**What changed.** Recorded the existing project/task authorization behaviour
before anything touches it, so the consolidation has a safety net.

Two artifacts:

- A generated matrix of 960 rows over `(actor) × (visibility) × (project
  department) × (created_by, current_owner, collaborator, assignee)`, each row
  eight real predicate calls, diffed against a checked-in baseline.
- Eleven named tests pinning the behaviours that later work will change, each
  carrying a `KNOWN DEFECT` docstring naming the decision that changes it.

**Files touched.**

- `crm_backend/projects_and_tasks/test_access_matrix.py` (new)
- `crm_backend/projects_and_tasks/access_matrix_baseline.txt` (new, generated)
- `docs/PERMISSION_INVENTORY.md` (new)
- `docs/BUILD_LOG.md` (new)

**Judgment calls.**

1. **Golden-file matrix rather than hand-written expectations.** A
   hand-written table for 960 combinations would be a re-implementation of the
   rules, and could be wrong in exactly the way the code is wrong — which
   defeats the purpose. The matrix calls the real predicates and records the
   answers; the baseline is plain text, sorted and aligned, so a behaviour
   change shows up as a readable diff. That diff is the review artifact.
2. **The matrix covers predicates, not endpoints.** Endpoints layer ordering,
   validation and writes on top; running 960 endpoint calls would be slow and
   would mostly re-test validation. The gates that live inline in
   `update_project`/`create_task` instead get named tests, listed under "Gates
   that are not predicates" in the inventory.
3. **Actor set includes a member of a second company.** Not in the dimension
   list given, but tenant isolation is the one property that must hold across
   every other dimension, and adding the actor is what surfaced D10.
4. **The matrix world gives the company owner no `CompanyUserProfile`.** That
   is the legacy shape the Owner-backfill work removes, so the baseline has to
   record how it behaves first. Expect this baseline to move when the backfill
   lands. **It did not** -- see WP21. `company_standing()` short-circuits on
   ownership before reading any profile row, so the access layer never depended
   on the row existing. The prediction was wrong in a useful way: it located the
   defect as a roster problem rather than a permission one.

**Deferred.** Nothing.

**Contradictions with the reference documents.** The prompt describes the
frontend as needing "a single `useProjectAccess(project)` hook replacing every
inline role check", implying scattered checks. It is already consolidated:
`usePermissions()` wraps `lib/permissions.ts`, `lib/projectPermissions.ts` and
`lib/eventPermissions.ts`, and components consume the composable. WP20 is an
extension of that structure to three access levels, not a cleanup of scatter.

---

## WP2 — Append-only audit log

**What changed.** A new `audit` app holding `AuditEvent` and the single writer
`record_event()`, wired into the four consequential mutations that already
exist. The rest (deadline changes, visibility changes) arrive with the work
packages that introduce them.

Workroom now has three records of "something happened", and the module
docstring states the distinction so nobody adds a fourth by accident:
`CompanyActivity` is a curated, skimmable company feed; `Notification` is
per-recipient and permission-aware; `AuditEvent` is complete, append-only, and
answers "who changed this, when, from what, to what, and why".

Wired now: member role change, deactivation, reactivation, removal, project
ownership transfer, task approval, task approval rejection.

**Files touched.**

- `crm_backend/audit/{__init__,apps,models,services,admin,tests}.py` (new)
- `crm_backend/audit/migrations/0001_initial.py` (new)
- `crm_backend/crm_backend/settings/base.py` — registered the app
- `crm_backend/users/services.py` — role change, active status, removal
- `crm_backend/projects_and_tasks/services.py` — ownership transfer, approve, reject

**Judgment calls.**

1. **`record_event` lets failures propagate rather than swallowing them.** The
   obvious design is catch-log-return-None so a broken audit write never fails
   a business operation. It does not work: callers record inside the
   transaction that made the change, and once a statement errors there,
   Postgres refuses every subsequent statement in that transaction anyway. The
   real choice is "fail loudly or fail confusingly".
2. **`action` is a `TextChoices` catalog, not free text.** The prompt shows
   `action = CharField()`. An audit trail whose action names drift cannot be
   queried, and querying is the only reason it exists.
3. **Append-only is enforced at three layers, not in the database.** The
   instance (`save`/`delete`), the manager (`update`/`delete`), and the admin
   (all three permissions refused). No trigger: the brief asks for append-only
   "in code or in Django admin", and a trigger would also block a future
   retention/purge job — a decision to take when retention is designed, not one
   to foreclose here.
4. **A cascade from `Company` still removes rows, deliberately.** Django's
   deletion collector issues its own SQL and bypasses the manager. A tenant's
   trail should not outlive the tenant and a deletion request has to be
   satisfiable. Documented on the model and covered by a test so it is a choice
   rather than a hole someone finds later.
5. **The rejection comment is deliberately excluded from the audit row.**
   Rejection comments are visible only to the submitter, and PRESERVE EXACTLY
   requires that. Copying one into a company-scoped audit row would route
   around it the day the audit trail grows a UI. The row records that a
   rejection happened, not what was said. Covered by a test.
6. **`before`/`after` hold only the changed fields, and model instances are
   reduced to their primary key.** An audit table that copies whole records
   becomes a second, unmanaged store of data that was deleted elsewhere for a
   reason.
7. **Member removal is recorded against the `User`, not the
   `CompanyUserProfile`.** The profile row is about to stop existing, and "what
   happened to this person" has to stay answerable afterwards.

**Deferred.** Deadline-change and visibility-change events, which land with
WP5 and WP6. No audit read API, UI, export or retention policy — all out of
scope by the brief.

**Tests.** 25 in `audit/tests.py`: one per wired action, no-op changes writing
no row, append-only refused at every layer with the row verified unchanged
afterwards, the company cascade, JSON coercion, and the rejection-comment
exclusion.

---

## WP3a — A per-project reference grants nothing to a non-member

**Why this came first.** WP3's job is to extract `resolve_project_access` as an
ordered `VIEW < CONTRIBUTE < MANAGE` level. Before extracting, the baseline was
checked for whether MANAGE already implies VIEW, because an ordered type cannot
represent "manage but not view". It did not: **66 rows granted MANAGE without
VIEW**.

Breaking those 66 down decided the order of work:

- **36 were the outsider** — a member of another company holding `created_by`
  or `current_owner`. Those must *lose* MANAGE, not gain VIEW. Rolling them
  into an ordered enum would have widened a cross-tenant hole rather than
  closing it.
- **30 were a DL or DM** who could genuinely edit, archive and transfer a
  private project that `GET` returned 403 for.

So the cross-tenant half is fixed here, on its own, with its own baseline diff.
The remaining coherence half is WP3's to absorb.

**What changed.** Every project/task predicate now resolves company membership
*before* consulting `created_by`, `current_owner`, `assigned_to` or
`task.created_by`:

`user_can_view_project`, `user_can_manage_project`, `user_can_manage_task`,
`user_can_approve_task`, `user_can_extend_deadline`,
`user_can_update_task_status`, `user_can_log_time`, `user_can_delete_time_log`,
and the assignee guard inlined in `submit_task_for_approval` (now delegating to
`user_can_update_task_status` rather than repeating the comparison).

`public` is deliberately still checked *before* membership: that visibility
exists to put a project outside the tenant boundary. Restricting who may set it
is a separate decision, enforced at the transition.

**Was this a live breach?** No, and the commit says so rather than overclaiming.
Assignees, collaborators and new project owners are all validated against the
company today, so no API path sets a cross-tenant reference. It was a missing
backstop.

It was not purely theoretical, though, and this is the part worth attention: a
reference can outlive the membership behind it. `created_by` is `SET_NULL` when
a *user* is deleted, but is deliberately left untouched when someone is merely
**removed from the company** — it is immutable provenance. Before this change,
a removed member kept `view` over every project they had created, and `manage`
over every task. Two tests pin exactly that, one for removal and one for
deactivation.

**Baseline diff: 192 rows, every one of them an outsider row.** No Owner, CM,
DL or DM row moved. Outsider rows are now all-zero except `view=1` on public
projects. That the diff is entirely confined to the actor the change was aimed
at is the evidence the change is scoped.

**Files touched.**

- `crm_backend/projects_and_tasks/services.py` — the eight predicates and the
  submission guard
- `crm_backend/projects_and_tasks/access_matrix_baseline.txt` — regenerated
- `crm_backend/projects_and_tasks/test_access_matrix.py` — D10's pinned test
  rewritten from defect to fix, plus three new tests (removed member,
  deactivated member, public still cross-tenant readable)

**Cost.** Predicates that previously short-circuited on an in-memory attribute
now issue one membership query first. WP3's resolver consolidates that into a
single lookup per request.

**One regression, caught by the full suite and fixed.** Adding the membership
check to `user_can_delete_time_log` made it traverse `log.task.project.company`
— a lazy foreign-key load inside an async context, which Django raises
`SynchronousOnlyOperation` for rather than merely running slowly. The fix is
not to prefetch at that call site but to change the signature to take `task`
explicitly: every caller already holds it with `project__company` selected, so
the traversal should never have been there. Same hazard checked across the
other predicates that newly touch `.company` (`user_can_approve_task`,
`user_can_extend_deadline`) — all their callers load through
`get_task_for_user` or `get_project_for_user`, both of which select the company.

This is the argument for the resolver in one paragraph: eight predicates each
deciding independently what they need loaded is how that class of bug gets in.

---

## WP4 — ProjectMembership and the four access levels

**What changed.** Visibility now grants discovery and nothing else. Anything
beyond VIEW is named explicitly, per person, on a `ProjectMembership` row
(viewer / contributor / manager, one per person per project, DB-constrained).

The resolver implements the full model: Owner/CM, a DL of the project's own
department, `current_owner` and a manager membership grant MANAGE; a
contributor membership or **being assigned a live task** grants CONTRIBUTE; a
viewer membership, a visibility match, or `created_by` grants VIEW.

**Judgment calls.**

1. **`created_by` drops from MANAGE to VIEW.** It is permanent, immutable
   provenance — it records who started the project, which is worth keeping
   forever, but it is not a claim on the project and could never be taken away
   from someone who should no longer have it. Nothing in the suite broke,
   because `create_project` also sets `current_owner`; what actually changes is
   that transferring ownership away now genuinely removes the old creator's
   grip.
2. **An assignment is itself a grant.** Somebody with MANAGE decided this
   person should do this task, and they cannot do it without reaching the
   project. Live tasks only.
3. **The backfill has two halves and the second is the important one.**
   Collaborators become contributor rows. But every project's `created_by` also
   gets an explicit manager membership — without it, every project run by the
   person who created it would silently lose its manager the moment this ships.
   Idempotent; the reverse only removes rows it could have created.
4. **`Project.collaborators` is deprecated, not dropped.** Kept for one
   release, dual-written by a single transactional function so the two stores
   cannot drift. Only contributor rows are touched: a viewer or manager grant
   was made deliberately and is not the collaborator field's to remove.
5. **The matrix fixture was restructured.** A membership row and a task
   assignment live in the database and cannot be simulated by mutating an
   instance. Now 1440 rows over membership role × relation × assignee.

**Deferred.** The members panel (frontend) and the endpoints to grant and
revoke memberships — WP7 and WP20. The backfill plus dual-write means nothing
regresses in the meantime.

---

## WP5 — Deadlines and late submission

**What changed.** §2's deadline decisions, in one package because they are one
story: the deadline rules were built around protecting a date rather than
recording what happened to it.

- The task/project invariant is now **inclusive** (`<=`). A task may land
  exactly on the project deadline.
- **The AI's one-hour buffer is deleted.** It existed only so generated tasks
  would satisfy the strict inequality; they now land on the real date with
  nothing for a human to correct afterwards.
- A new task's deadline **defaults to the project deadline** instead of being
  required.
- Deadline changes belong to **whoever can manage the project**, move in either
  direction, **require a stated reason**, and are audited through
  `record_event`.
- Shortening is blocked only by the task invariant, and returns **409 with the
  offending tasks** so the UI can list them and let someone fix them inline.
- **Late submission is allowed and flagged** (`TaskApproval.submitted_late` /
  `late_by`); every other submission guard is unchanged.
- A project deadline change now notifies **everyone holding a live task**, not
  only the current owner.

**Judgment calls.**

1. **Lateness is recorded on the approval, not the task.** A task can be
   submitted late, rejected, and resubmitted on time; both facts are true of
   their own attempt. Overdue stays a derived state — no status value, no extra
   Kanban column.
2. **The reason is required, and it travels.** Widening who may move a deadline
   needs a counterweight; a date changing under the people doing the work with
   no explanation is what makes a deadline feel arbitrary. The reason goes into
   the audit row *and* into the notification.
3. **`extend-deadline` became `change-deadline`.** A breaking API change, taken
   rather than keeping a name that describes half of what the endpoint does.
   The frontend needs the new path plus a required `reason` field.
4. **`Notification.Type.DEADLINE_EXTENDED` keeps its stored value.** The label
   now reads "Deadline Changed" and the message says which way it moved.
   Changing the stored value would need a data migration over existing
   notifications for no user-visible benefit.
5. **Shortening returns the blockers, following `remove_member`'s existing
   convention** of returning a blockers payload alongside its error rather than
   inventing a new response shape.

**Tests.** The old deadline test class encoded the rules this reverses, so it
was rewritten rather than patched: manager-can, current-owner-can (the case the
old rule got wrong), CM-can, unrelated-member-cannot, reason required, pulling
in allowed, equality allowed, one second past refused, shortening returns the
blocking tasks, the change is audited with its reason, and assignees are
notified. Four characterization expectations moved from `KNOWN DEFECT` to
`FIXED`, each with the reasoning in its docstring.

Also added a test that re-checks the submission guards this package did **not**
touch. Removing one guard is exactly the moment another quietly goes.

**One test artifact worth recording.** `refresh_from_db()` clears cached
relations, so a later `task.project` lazy-loaded inside the event loop and
raised `SynchronousOnlyOperation` — the same class of failure as WP3a's, in a
test this time. Fixed by re-fetching with `select_related`, not by making the
service defensively prefetch: the async service layer expects its objects to
arrive loaded, exactly as the routers supply them, and hiding that requirement
would just move the failure somewhere less obvious.

---

## WP6 — Visibility escalation, the public gate, and one shape for approvals

**What changed.** §1's visibility decisions. Visibility decides who can *find*
a project; three of its four levels keep the project inside the company and one
does not, and until now all four were guarded by the same rule.

- **`can_set_visibility` is the one place a visibility transition is decided.**
  `update_project` no longer carries an inline role check, and both the direct
  path and the approval path go through it.
- **`public` is gated twice**: `Company.allow_public_projects` must be on, and
  the caller must be Owner or Company Manager. Off by default.
- **`company` visibility needs a Department Leader at minimum**, and a DL only
  for a project in their *own* department.
- **`private` ↔ `department` is ungated** beyond ordinary project management.
- **Creation answers to the same gate.** Asking for `public` in the create call
  is checked against the company, since there is no project yet to check.
- **`department` visibility requires the project to have a department**, on both
  the create and the update path.
- **Every visibility change is audited** through `record_event`, from both
  paths, with before and after.
- **`ApprovalRequest`** replaces the pattern `ProjectVisibilityRequest` started.
  Rows are migrated (0014); the old model is marked deprecated and its table
  kept.
- **`GET`/`PATCH /company/settings/`** — the Owner's switch for
  `allow_public_projects`. Readable by any member, writable only by the Owner,
  audited under a new `company.settings_changed` action.

**Judgment calls.**

1. **The public gate is a company switch, not a permission.** Publishing is a
   decision about the company's own exposure, so it is made once by someone who
   owns that risk rather than implicitly by whoever happens to manage a project.
   A per-project permission would have put the decision in the hands of the
   person with the least context about it.
2. **The flag is checked before the role**, so a Department Member sees "this
   company does not publish projects" rather than "you personally may not". The
   first is true and actionable; the second implies that asking someone more
   senior would help, when it would not.
3. **Lowering visibility is never gated.** Gating it would mean a project could
   get stuck published — the failure mode worth avoiding is the one where
   reducing exposure requires a meeting.
4. **Existing public projects stay public.** `allow_public_projects` defaults to
   off, but silently unpublishing a project someone is actively sharing would be
   a worse surprise than leaving it. The gate applies to *new* transitions; 0014
   logs the affected rows at WARNING for review at deploy time.
5. **Approving re-checks the reviewer against the target.** A request records
   what someone asked for; it never authorizes the change on its own. A request
   created when the rules were looser cannot be approved into a state the rules
   now forbid.
6. **`ApprovalRequest.payload` is JSON, and never applied directly.** The *ask*
   differs per kind while the workflow does not. Approving runs the same
   validated service a direct action would, so a stale or malformed payload
   cannot become a change nobody checked.
7. **The reviewer is advisory, not exclusive.** Resolved once at creation so a
   request is never left with nobody able to act on it, but anyone with
   authority over the target may decide it — which is what stops a request dying
   because one named person is on holiday.
8. **The Owner's switch is narrower than every other admin endpoint.**
   `update_company_settings` resolves through `get_owned_company`, not
   `get_managed_company` — the latter also admits Company Managers and
   department leaders. Publishing decides whether the company's work can leave
   the company, so it stops at the one person accountable for that. Reading the
   setting is open to any member, because the project form has to know whether
   `public` is on the menu before it offers it.

**Three gaps found while finishing the package.** All three were in the code as
first written, and each is the kind that a green test suite does not catch:

1. **The gate had no key.** `allow_public_projects` was read by
   `can_set_visibility` and written by nothing — no endpoint, no admin path. The
   403 told the user "the Owner has to switch it on first" while `public` was in
   fact unreachable for every company, permanently. Hence
   `/company/settings/`, and `test_switching_it_on_is_what_makes_a_project_publishable`,
   which asserts the whole loop rather than either end of it.
2. **`department` visibility with no department.** `VISIBILITY_ESCALATION_ROLES`
   maps `department` to `None`, meaning ungated, so a project with no department
   could be moved there — and `resolve_project_access` guards on
   `project.department_id`, so the result granted view to nobody. The project
   read as shared while behaving exactly like `private`. The prompt says
   "must have a department"; now both paths enforce it.
3. **The approval refusal returned 200.** Judgment call 5 added error returns to
   `approve_visibility_request`, but the router mapped only `forbidden` and
   `not_pending`. The new refusals fell through to the success line and rendered
   a `None` request. Reachable: an Owner can clear a project's department while a
   request against it is pending. Both refusals are now mapped, with a test that
   drives exactly that sequence.

**A rule this deliberately reverses.** A Department Member could previously
never change visibility directly, even downward. They now can, for
`private` ↔ `department`, when they manage the project — and since
`create_project` sets `current_owner=user`, a DM managing their own project is
the common case. That makes `request_visibility_change` largely redundant for
the case it was written for. It is retained and still correct: it serves a DM
who *created* a project but does not manage it, which after WP4 is a real state
(`created_by` grants VIEW, not MANAGE).

**Deferred.** The visibility-request endpoints still write
`ProjectVisibilityRequest`, not `ApprovalRequest`. Moving them is a router and
service change with its own tests, and doing it in this package would have mixed
a data-model migration with an API change. The model docstring records the order:
move the endpoints, then drop the table.

**Also worth recording.** 0014's `already` guard compares `(target_id,
created_at)`, but `ApprovalRequest.created_at` is `auto_now_add`, so a backfilled
row never carries the original timestamp and the guard cannot match. It is
unreachable in practice — Django records migration completion, and `backwards`
deletes what `forwards` wrote — but it reads as protection it does not provide.
Left as-is rather than churning an already-tested migration; noted so it is not
trusted later.

**Tests.** `projects_and_tasks/test_visibility.py`, 23: the flag refuses
`public` while off; Owner and CM can publish once on; a DL cannot publish even
when allowed; a DM cannot publish a project they manage; the refusal message
names the company rule, not the caller's rank; creating as `public` answers to
the same gate; a DL can take their own department's project company-wide but not
another's; a DM cannot; `private` ↔ `department` needs only management; lowering
is never gated; every change is audited; a refused change and a no-op change
each write no audit row; another company cannot touch visibility; the flag is
read from the project's company, not the caller's; approval re-checks authority;
approval is audited; a departmentless project is refused `department` visibility
on both paths and writes no audit row; and approving a request whose project lost
its department is a clean 400 rather than a 200 over a `None`.

`company/test_settings.py`, 11: any member reads, only the Owner writes — a
Company Manager and a Department Member are both refused; someone with no
company gets a 404; another company's Owner writes only their own company; an
absent field and an explicit `null` both leave the setting alone; the change is
audited with both sides and a no-op records nothing; and the end-to-end loop —
refused, switched on, accepted.

**Two characterization expectations moved from `KNOWN DEFECT` to `FIXED`**, each
rewritten with the reasoning rather than deleted, and each split in two because
the new rule is two rules:
`test_a_department_member_can_publish_a_project_they_manage` became "managing no
longer lets you publish" plus "even with the flag on, a DL still cannot"; and
`test_a_department_member_cannot_change_visibility_even_when_they_manage` became
"a DM who manages may now move it inside the department" plus "still cannot take
it company-wide". `test_a_public_project_is_readable_by_a_member_of_another_company`
keeps its assertion and gets a new docstring: the read half of D7 stands by
design, and is pinned so that narrowing it would be deliberate.

`api/tests.py::test_public_project_visible_across_companies` needed the same
treatment for a different reason: it asserts what `public` *means*, which is
unchanged, but it got there by creating a public project — now refused while the
company disallows it. The flag is switched on in the test, and the refusal it
used to depend on implicitly is now asserted on purpose in a test beside it.

Full suite: **609 passed, 1 failed** — `AIHealthSummarySecurityTests::
test_rate_limit_boundary`, which needs a real Redis and fails identically on
`main`. That is the known baseline, not a regression.

---

## WP7a — Field-level authority on a task

§2 asks for a task's fields to answer to different people. Before this, they
answered to one question -- `user_can_manage_task` -- and it was the wrong one
twice over: it granted on `task.created_by`, and it was all-or-nothing.

### created_by stops being a claim

`user_can_manage_task` short-circuited on `task.created_by`, so whoever raised
a piece of work kept edit/assign/archive rights over it forever. That is D2,
and it is the same correction `Project.created_by` got in WP4: provenance is
worth keeping permanently and is not a standing claim.

The baseline priced it before the change, which is the point of having one:
**240 of the 1440 characterized combinations granted management of a task on a
project the same person could not open.** All 240 were DL (96) or DM (144) --
never Owner or CM, who hold MANAGE by role anyway. The concrete case: a
Department Leader creates a task, the project later moves to another
department, and they keep editing, reassigning and archiving work inside a
department they have left.

The regenerated baseline is the review artifact and its shape is the argument:
240 rows changed, **one column** (`own_task`), **one direction** (1 -> 0), no
other column touched. The predicted blast radius and the actual one matched
exactly.

`test_task_creator_keeps_managing_a_task_they_cannot_otherwise_touch` was a
KNOWN DEFECT characterization test asserting the bug. It is now
`test_managing_a_task_comes_from_the_project_not_from_having_created_it`,
asserting the opposite, with the history in its docstring -- the same treatment
D3 got in WP5.

### Fields split two ways

    TASK_ASSIGNEE_FIELDS   description, estimated_time
    TASK_MANAGE_FIELDS     title, priority, department_id, task_type_id

The assignee owns how the work gets done; management owns what the work *is*.
Authority is decided against the fields a request actually carries, not against
the task as a whole.

A request mixing the two is refused **whole**, never half-applied. Letting the
permitted half through would make the result of a request depend on which
fields it happened to carry, and would apply half an edit the caller sent as
one change.

### Two routes around the rules, closed

**A deadline could be moved with no reason and no audit row.** WP5 built
`change-deadline` -- MANAGE, required reason, audit row, notification -- and
left `deadline` writable through the ordinary PATCH, where none of those four
applied. Choosing the other endpoint was enough to skip all of them. `deadline`
is now in neither field set.

It is still declared on `TaskUpdateIn`, which looks redundant and is not:
Ninja ignores unknown fields, so removing it would have turned a request that
means "move this deadline" into a silent 200 that moved nothing. It is accepted
in order to be refused, with a message naming the endpoint to use instead.

**A task with a pending submission could not be reassigned at all.** Blocking
is the wrong way round: the usual reason to reassign mid-review is that the
original assignee has gone or is stuck, which is precisely when their
submission will never be resolved. The task wedged in In Review with nobody
able to move it.

Reassignment now voids the submission: `TaskApproval.STATUS.VOIDED`, the task
drops back to In Progress, the person who submitted is told their evidence will
not be read, and both the void and the assignee change are audited.

VOIDED is a new status rather than REJECTED because rejected is a judgement on
the work and nobody read this work. Putting a rejection on someone's record for
a submission that was never opened would be a small injustice the data model
would then make permanent.

Reassigning to the person who already holds the task is a no-op and voids
nothing -- otherwise a stray double-click would close a live submission.

### The void is not transactional, deliberately

`assign_task` writes the assignment, an audit row, the void, a second audit row
and a notification as five separate statements. If one fails partway, the task
can be left reassigned with the previous assignee's submission still pending.

Django transactions are sync-only and this whole service layer is async, so
there is no `atomic()` to reach for here -- `submit_task_for_approval` has the
same shape and predates this work. Wrapping just this one path in
`sync_to_async(transaction.atomic)` would introduce a second convention for
multi-step writes in the same module, which is worse than one honest gap.

The failure mode is recoverable rather than corrupting: a pending approval
belonging to somebody who no longer holds the task, which the next reassignment
or a submission by the new assignee resolves. Worth fixing properly when the
async service layer gets a transaction convention, not before.

### Deliberately not in this package

Task creation still requires only VIEW (D1). Moving it behind MANAGE without
shipping the "Propose task" flow in the same commit would leave contributors
with no way to raise work at all, so both land together in WP7b.

**26 new tests** in `projects_and_tasks/test_task_authority.py`.

The suite caught one defect in this package that its own tests did not:
`NotificationEmailTemplateMappingTests` requires every `Notification.Type` to
map to its own email template, and `TASK_SUBMISSION_VOIDED` had none -- it would
have silently fallen back to the generic template. That test is doing real work;
the new template is deliberately blue rather than the amber "changes requested"
treatment, because nobody judged the work and an email that reads like a
rejection would tell the recipient something untrue about it.

Full suite: **635 passed, 1 failed** in 8m41s -- the failure is
`AIHealthSummarySecurityTests::test_rate_limit_boundary`, which needs a real
Redis. Known baseline, not a regression.

---

## WP7b — Task creation behind MANAGE, and the route that replaces it

D1 was the last place visibility still conferred a capability. `create_task`
was gated on `user_can_view_project`, so `company` visibility -- meant to grant
discovery and nothing else -- let any member of the company add work to any
project they could find, and choose who it was assigned to.

Fixing it alone would have been a regression, not a fix: contributors would
have had no way to raise work at all. Creation moving behind MANAGE and the
"Propose task" flow that replaces it are two halves of one rule, so they land
in one commit. That is why WP7 was split -- WP7a could stand alone, this
could not.

### A proposal is an ApprovalRequest, not a draft Task

The tempting shortcut is a `Task` with `status="proposed"`. It does not
survive contact with the rest of the system: an unaccepted proposal would
appear on the board, count toward project progress, be assignable, and be
reachable by every query that already filters tasks by project. Every one of
those would need a new exclusion, and the first one anybody forgot would be a
bug nobody noticed.

`ApprovalRequest(kind="task_proposal")` was already the right shape from WP6.

### The pending-uniqueness constraint had to be scoped per kind

WP6 wrote `one_pending_approval_request_per_target` over
`(kind, target_type, target_id)` where status is pending. Correct for
visibility -- a project has one visibility, and two pending requests to change
it is not a state anyone can reason about.

Applied to task proposals it would have been badly wrong: the target is the
*project*, so exactly one person could hold exactly one proposal open at a
time, per project. The condition now names the kind it applies to. Whether two
open asks may coexist is a property of the kind, not of the workflow.

### created_by is the accepting manager, not the proposer

This looks like it loses provenance and does the opposite of WP7a. It is the
one place the two rules meet.

`created_by` still heads the approval fallback chain in
`user_can_approve_task`. Setting it to the proposer would make a contributor
the approver of work they suggested and may well be assigned -- inventing a
fresh self-approval path in the same package that is trying to close one
(R5). The proposer is recorded permanently as `requested_by` on the request,
which is where a proposal's provenance actually belongs.

### Accepting re-validates, and does not trust the payload

`accept_task_proposal` calls `create_task` with the accepting manager as the
actor rather than writing the payload into the tasks table. This is the rule
`ApprovalRequest` was designed around and it is load-bearing here: a proposal
written against one project deadline must not become a task that violates the
deadline the project has now. Two tests pin it -- one moves the deadline under
a pending proposal, one writes `assigned_to_id` and `status: Done` directly
into a stored payload and checks neither reaches the task.

The payload is allow-listed on the way in *and* on the way out. A payload
written by an older release must not be able to reach `create_task` carrying a
field this one does not understand.

### Break this into steps

`POST /todos/tasks/{id}/steps/`, capped at 20. This is where most of the
pressure to let anybody create tasks was actually coming from: "I need to
track the three things this task breaks into" is a real need, and answering it
by widening task creation would have put somebody's private working notes on
the company board.

To-dos were already the right home -- private to one person, invisible to
every role including the company Owner, outside analytics -- so this is a thin
batch endpoint over `get_assignable_task` and `create_todo`, and it inherits
every privacy rule those already have, including the revoked-link behaviour
when the task is reassigned away.

Steps default to the task's own deadline rather than today, falling back to
today when that has passed. Today is only the right answer on the last day,
and a to-do dated in the past sorts above everything and reads as overdue the
moment it is created.

### Judgment call: a manager proposing gets a 400, not a queued proposal

Accepting it silently would leave a manager's proposal queued for the very
person who filed it. The 400 says to create it directly.

**34 tests** in `projects_and_tasks/test_task_proposals.py`, **16** in
`todos/test_task_steps.py`.

Full suite: **685 passed, 1 failed** in 7m48s -- the failure is the Redis
one. Only one stale expectation turned up, and it was the D1 KNOWN DEFECT
test, rewritten to assert the opposite as D2 was in WP7a.

---

## WP21 — Every owner is a member

Companies created before `register_company_in_transaction` started writing an
Owner-role `CompanyUserProfile` had their owner exist as `Company.owner` and
nowhere else. Everything that built a list of people from `CompanyUserProfile`
therefore omitted the one person who could not be omitted, and each place grew
its own compensation:

- `get_company_stats` added `+1` to the member count when the owner had no row.
- `get_company_workload` **synthesized a placeholder roster entry** for them --
  the one person guaranteed to be in every company was assembled by different
  code from everybody else.
- `_should_email` read `None` for their notification preference and defaulted
  to enabled, so an owner could not turn company email off.
- Four member endpoints returned "The company owner has no profile."

Users migration `0008` backfills the missing rows and all of that goes.

### The migration adds, never edits

It inserts only for companies with no row for their owner. An owner who already
holds a profile at an unexpected role -- reachable through ownership transfer --
is left alone and logged at WARNING. Demoting or promoting somebody is a
decision, not a data fix, and a migration is the worst possible place to make
one silently.

`backwards` is deliberately a no-op: the rows it creates are indistinguishable
from the ones registration writes, and deleting an owner's membership would
strip their notification preferences and take them off the roster -- exactly the
breakage this exists to fix.

### The fixtures were building an impossible world

`TwoCompanyTestCase` and the access-matrix world both created companies with
`Company.objects.create()` and no owner profile. After this migration that shape
cannot occur, so every test standing on those fixtures was exercising a state
the system no longer produces. Both now create the Owner profile.

`test_owner_with_no_profile_row_gets_no_profile_error` asserted the defect
directly -- that an owner fetching their own profile got a 400. It now asserts
they get a 200, with the history in its docstring.

### The baseline did not move, and that is the interesting part

WP1 predicted this backfill would shift the access matrix, because the matrix
world deliberately withheld the owner's profile. Regenerating produced **a zero
diff**.

The reason is worth recording: `company_standing()` short-circuits on
`company.owner_id == user.id` before it ever reads a profile row, so the
permission layer never depended on the row existing. Only the *consumer* sites
did -- roster, count, pool, preference. The defect was never an access defect;
it was a "the owner is missing from lists" defect, which is why it showed up as
four unrelated-looking branches rather than as a permission bug.

### Deliberate deviation: the permission short-circuits stay

§10 says to delete every branch handling "owner might have no profile". The four
consumer branches are gone. The `company.owner_id == user.id` short-circuits in
`company/services.py` and `access.py` are **kept**, which is a deviation and is
deliberate.

They are no longer load-bearing -- with the backfill, resolving through the
profile row would give the same answer. But they are the only thing standing
between a missing or inactive owner row and an owner locked out of their own
company, which is the most destructive failure this system has. Trading a
guaranteed-correct code path for one guaranteed by data is not a trade worth
making for the sake of deleting four lines, especially when the same rule
appears under PRESERVE EXACTLY as "Owner never a valid removal/demotion target".

If they should go, that is a separate decision with its own test pass, not a
side effect of a backfill.

**11 tests** in `users/test_owner_profile_backfill.py`, covering the backfill
against a legacy fixture, its idempotency, that it never edits an existing row,
and each of the four consumer sites now that its branch is gone.

Four stale expectations, all of them pinning the defect rather than a contract:
an owner fetching their own profile got a 400; an owner had no notification
preference to set; and two asserted `profession is None`, which was only ever
true of the synthesized placeholder row. All four rewritten with the history in
their docstrings.

Full suite: **696 passed, 1 failed** in 8m39s -- the Redis one.

---

## WP22 — Company context resolves to a membership

Every resolver in `company/services.py` answered "which company", which quietly
assumed the answer was unique. It is today -- nothing in the product joins a
second company -- but the assumption sat in every call site rather than in one,
and the `User`/`CompanyUserProfile` split exists precisely so it would not have
to hold forever (§13).

`resolve_company_context(user, company_id=None) -> CompanyContext | None` states
it once, and returns the membership row alongside the company.

### The id is a selector, not a grant

This is the part worth being careful about, because taking a `company_id` from
a caller looks exactly like the thing NON-NEGOTIABLE RULE 1 forbids.

It is not the same thing: the id chooses **among the caller's own
memberships**, and resolves to `None` for anything else. A company the caller
does not belong to, a deactivated membership, and an id matching nothing all
produce the same answer -- indistinguishable, so the parameter cannot be used
to probe which companies exist. Four tests pin exactly that.

Nothing passes an id today, so this changes no behaviour. It means a future
company switcher does not require touching every call site again.

### `membership`, not just `role`

Callers that have the context almost always want more from it than the role:
the department now, the notification preference already, capacity when §3
lands. WP21 is what makes this clean -- every owner holds a membership row, so
the context can always carry one rather than having a hole in it exactly where
the most privileged user is.

`department_id` is the one derived field, and it returns `None` for the owner
whatever their profile says, matching `get_member_department_id`.

### One fallback implementation, and it is now deterministic

`get_member_company` delegates rather than keeping a second copy of the
ownership-first-then-membership rule. Its contract is unchanged and a test
pins all three cases.

One quiet fix on the way through: the old `.afirst()` had no `order_by`, so a
user holding two memberships got whichever row the database chose. Not
reachable through the product, but it would have been the first thing to go
wrong the day it was. The fallback is now the oldest membership, explicitly.

**20 tests** in `company/test_company_context.py`.

Full suite: **716 passed, 1 failed** in 8m10s -- the Redis one. No stale
expectations, which is the result a behaviour-preserving change should have.

---

## WP8 — Task dependencies

One model, two kinds, one project. `blocks` is hard and has exactly one
observable consequence: the successor cannot leave To Do until the predecessor
is Done. `relates_to` is informational and gates nothing.

Explicitly not built, per §2: SS/FF/SF, lag, critical path, cross-project
edges. Two kinds can be explained in a sentence, and only one of them does
anything.

### The block is checked at the transition, never cached

There is no `is_blocked` flag on `Task`, deliberately. A cached one is wrong
from the instant a predecessor moves, and while it is wrong it either strands
somebody on work that is ready or waves through work that is not. The check
runs in `update_task_status`, on the To Do -> anything transition, against the
predecessors' current state.

The service returns the unfinished predecessors rather than a bare error, and
the endpoint renders their titles. A block with no explanation is
indistinguishable from a bug.

An archived predecessor blocks nothing -- there is no route left to complete
it, so leaving it in the way would strand the successor permanently.

### The cycle race is real corruption, so it gets a lock

This is the decision the package turns on.

Adding an edge is a read-then-write: walk the graph, conclude the edge is
safe, insert. Two people adding A->B and B->A at the same moment each read a
graph without the other's edge, each conclude they are safe, and both insert.
The result is two tasks permanently blocking each other, from data that was
valid when each request checked it, with no route out through the product.

Most races in this codebase leave an inconsistency something later resolves --
see "The void is not transactional" under WP7a, where that was the reason to
leave a gap open. This one does not. So the check-and-insert runs inside one
transaction holding `pg_advisory_xact_lock` keyed on the project.

**Django transactions are sync-only, so the guarded part is a sync function
called through `sync_to_async`** -- the shape `persist_ai_generated_tasks` and
`users.services.update_member_role` already use. Following the existing
convention beat inventing a second one, which was the concern flagged when WP8
was queued.

Authorization is deliberately **outside** the transaction. It is a property of
`(user, project)` and is not racing with anything in the block, so it stays in
the async caller where `resolve_project_access` lives. Pulling it in would have
meant writing a sync copy of the access resolver -- duplicating the one place
the access model is defined, to serve a lock that does not need it.

The lock is keyed per project, derived from the project id with blake2b, since
Postgres advisory locks share one flat integer namespace across the database.
Cycles can only form within a project, so two projects never wait on each
other.

### The concurrency test was verified by breaking the code

A concurrency test that passes against broken code is worse than none. This one
was checked by replacing the `pg_advisory_xact_lock` call with `SELECT 1` and
re-running: both threads inserted, the assertion went from `['cycle', 'ok']` to
`['ok', 'ok']`, and a real cycle appeared in the table. Restored, it passes.

### The nightly check reports and never repairs

`find_dependency_cycles` runs at 03:30 as the backstop §2 asks for. In a
correct system it finds nothing -- the guarded path already refuses to close a
loop. It exists because a cycle can still arrive by a path that does not go
through that check (a data migration, a fixture load, a future bulk import, a
bug in the check itself), and a cycle is silent: nobody reports it, two tasks
simply never start, and the reason is invisible from either one.

It logs and stops there. Breaking a cycle means deleting somebody's edge, and
which edge is wrong is a judgment about the work -- not something a job should
decide at 3am with no one watching.

The walk is iterative rather than recursive, and a 300-task chain is tested,
because a long chain is exactly what a sequential plan looks like.

**33 tests** in `projects_and_tasks/test_dependencies.py`.

Full suite: **749 passed, 1 failed** in 9m23s -- the Redis one. No stale
expectations: dependencies are additive, and nothing existing assumed a task
could always leave To Do.

---

## WP14 — One document model, four scopes

§7 asks for one `Document` with a `scope` rather than four systems. There were
already two -- project `Attachment` rows and Info Portal folders -- and
personal and company files had nowhere to live at all.

### What moved, and what deliberately did not

`Attachment` was doing two jobs that look alike:

- **project and task documents** -- a file somebody filed. Migration
  `documents/0002` copies those onto `Document(scope="project")`.
- **task-approval evidence** (`approval` set) -- a file submitted for
  judgement inside one review cycle, part of an append-only history PRESERVE
  EXACTLY names. Those **stay** on `Attachment`.

A document is something you filed; evidence is something you submitted.
Merging them would have meant either evidence inheriting document deletion, or
documents inheriting evidence's immutability. Link and page attachments stay
too -- they hold no file, and giving a URL a `FileField` row with nothing in it
would be pretending otherwise.

Additive: nothing is deleted from `Attachment`, the copied rows are simply no
longer what the API reads. Same treatment as `Project.collaborators`.

### One access function, and one strict scope

`resolve_document_access` answers all four scopes. `personal` is checked first
and returns immediately, so there is no path by which a company-role check
below it could ever apply: §7 says no administrative override "including
Owner-over-CM or CM-over-Owner", and both directions are tested.

The listing filters in the query rather than after it. A listing that fetches
everything and then drops what the caller may not see is one forgotten call
away from being a leak, and it cannot be paginated correctly.

### One validator, several policies

§7 wants content-type and size validation "on every upload path, not just
approval evidence". There were four hand-written copies of the same two checks
-- documents, evidence, project images, résumés -- plus profile pictures in
`api.py`. They now all call `documents.services.validate_upload`, which takes
the limits as arguments. The differences between paths are real and worth
keeping; four copies free to drift were not. A zero-byte upload is now refused
too, which none of the copies did.

### Two things I broke and had to fix

Worth recording rather than quietly correcting:

1. **I overwrote `api/routers/documents.py` wholesale**, destroying the
   existing project-document endpoints including `download`. Recovered from
   git and rewritten so every original URL survives, backed by `Document`.
   The paths stayed because breaking every client to rename a model is a cost
   with no benefit.
2. **I dropped the `project__is_deleted=False` filter** the old lookup carried,
   which left documents reachable by direct id after their project was
   archived. Caught by `ProjectDeletionCascadeTests`, which existed for exactly
   this and earned its keep.

One stale expectation: a cross-tenant download asserted 403. It now answers
404, which is what NON-NEGOTIABLE RULE 1 requires and what a personal document
needs -- a document must not confirm its own existence to somebody who may not
read it.

**Retention**: soft delete records `deleted_at`, a nightly job purges past 30
days, and `restore` exists inside the window -- without it the window is only
a delay before permanent loss. Files are deleted one at a time rather than by
a bulk queryset delete, which would drop the rows and orphan every file behind
them.

**48 tests** in `documents/tests.py`.

---

## WP15 — Analytics tiers

Five tiers, in `analytics/tiers.py`, drawn wherever a number stops being about
a project and starts being about a person.

The endpoint §8 singles out is `/analytics/company/members/`: every
colleague's name against their open task counts, previously readable by **every
role**, including a Department Member. That is one join away from a performance
dashboard. It is now Owner/CM only.

Two boundaries worth explaining:

- **The department breakdown with no `department_id` is company-tier.** It is
  every department's numbers, and a Department Leader asking for it would be
  reading every other department's figures through a different door. With an
  id, a DL may ask about their own and no other.
- **`PROJECT_PEOPLE` counts only that project's tasks.** Showing someone's
  company-wide load there would leak the workload of projects the viewer has
  nothing to do with, through one they happen to manage.

`/analytics/me/` is new and never gated. Being able to see your own workload is
not a privilege, and making it one pushes people toward the company roster to
answer a question about themselves.

**Nothing per-person is computed.** §8 rules out on-time percentages, velocity,
productivity scores and rankings "not now, not later without a separate
explicit decision". `NoPerPersonRatesTests` walks every analytics response and
fails on a key containing any of those words, so adding one is a deliberate act
that breaks a test rather than a quiet addition to a serializer.

**39 tests** in `analytics/test_tiers.py`.

---

## WP16 — To-do timezone, eligibility and supersede

### A manual due date may no longer be in the past

Computed in the owner's timezone, at request time. The timezone half matters:
"today" for somebody in Addis is a different day from the server's for several
hours of every day, so a UTC comparison would reject a legitimate today or
accept a yesterday depending on when they happened to be working.

The past-date half **reverses** an existing deliberate decision -- the old
docstring argued backfilling yesterday is legitimate. §9 says otherwise and is
right: overdue is a state a to-do *arrives at*, not one worth being able to
create. The AI path keeps `allow_past`, because §9 requires overdue work to be
included and ranked first.

### The eligibility rule, and a reading I got wrong first

§9 lists six conditions. The `blocks` clause is only implementable because of
WP8.

I initially separated "the window the to-dos are dated in" from "the horizon
tasks are drawn from", on the reasoning that a `today`-mode list restricted to
tasks due today would tell somebody with a full fortnight of work that they
had none. That was me second-guessing the prompt. §9 states it twice --
"deadline is inside the window or already overdue", and "only exclude
completed, archived, and out-of-window-future work" -- so `today` mode is a
**daily focus list**, not a backlog, and that is a coherent product.

The consequence is real and handled: a person with plenty of work and nothing
due today gets an empty result, so the message says *nothing is due today*
rather than *no tasks assigned*, which would have been untrue.

### Supersede

One active plan per (user, task) and per (user, today). Asking again means
"that one was wrong", not "give me two lists", and two overlapping AI
checklists for one day is exactly the duplication people report as the feature
being broken.

Completed items are **kept and detached** -- the owner did that work, deleting
it would destroy their record rather than tidy ours, and detaching stops a
later dismissal of the old generation reaching back for them. Incomplete AI
items go. **Manual to-dos are never touched**, and the filter names the source
as well as the generation so even a mis-attached manual row survives.

Ten generations per user per day, counted in the user's own timezone so the cap
and the plan agree about which day it is, and checked **before** the provider
call rather than after.

**39 tests** in `todos/test_generation_rules.py`. Five stale expectations
updated, including one that asserted a past due date is allowed; the tests that
need an overdue to-do now build one the way reality does -- written directly,
past the endpoint that guards creation.

---

## WP17 — Plans, entitlements, usage counters

Everything §11 specifies, built and measured. **Enforcement defaults to off**,
which is the R1 decision and is recorded there rather than here.

### The switch, and why it landed where it did

I built this enforcing, as §11 demands, and ran the suite: **46 failures**.
Not subtle ones -- department creation, team creation, publishing a project,
company analytics. Every test company has no subscription, so it resolves to
Free, and Free allows one department, **zero teams** and no `public_projects`.

That is the R1 conflict stated as evidence rather than as a reading of three
documents. The product was built without limits; applying them is a decision
with real customer consequences on the day it ships, not a default somebody
inherits. `ENTITLEMENTS_ENFORCED` now defaults to `False`, matching what the
prompt's own OUT OF SCOPE list and `CLAUDE.md` §§15-16 both already asked for.

`has_feature` follows the same flag. The switch has to mean one thing --
either plan restrictions apply or they do not. Permissive on counts but
restrictive on features would be the worst of both: lenient where it is
measurable, and restrictive where it is visible.

Nothing else is deferred. Limits resolve, usage is counted and reconciled, and
every check reports the true numbers, so a UI can already show "5 of 3 used"
and an upgrade prompt. Turning enforcement on is an environment variable, and
the usage history needed to decide when -- the part that genuinely cannot be
reconstructed later -- is being collected now.

### Checks read rows, not counters

§11 describes incrementing a counter at each point of change and checking
against it. That works right up until one create path forgets to increment,
and then the limit is silently wrong in whichever direction the bug went.

So point-in-time metrics are counted **from the rows** at check time, and the
counter is a cache for dashboards, reconciled nightly. It is the same
"counters are for speed; rows are for truth" rule §11 states for
reconciliation, applied one step earlier -- and it deletes a class of bug
rather than scheduling a job to detect it. A test pins it: a counter drifted
to 999 cannot refuse anything.

Monthly metrics are the exception and do use the counter, because credits
spent leave no other trace. There is nothing to recompute them from. They
reset by period key, so a new month is a new row and no job has to fire at
midnight on the first.

### plan_snapshot

The field that makes the whole arrangement honest: limits are frozen at
subscription time and resolved **before** the live `Plan`, so editing `team`'s
numbers cannot silently change what an existing customer is paying for. An FK
alone cannot express that -- it follows the row wherever it goes.

A company with no subscription resolves to the seeded `free` plan. That is a
real, common state, and treating it as "no limits" would mean the limits apply
to paying customers and nobody else.

### Over-limit behaviour

Soft-block throughout. A downgrade to Free at 6/5 members keeps all six and
blocks the seventh; nothing is removed and nothing is archived. `past_due`
gets 14 days measured from `past_due_since` rather than `updated_at`, because
a grace period any unrelated write silently restarts is not a grace period --
and `past_due` with no recorded start is treated as *in* grace, since guessing
against the customer on missing data is how a billing bug becomes a support
incident.

Every refusal is a `402` naming the limit and the current number. A bare `403`
on a limit is unactionable.

**48 tests** in `entitlements/tests.py`, covering both switch positions, all
six scenarios §11 asks for, and the permissive default itself.

---

## WP18 — Integration seams

Models only. §12 asks for the shape and explicitly not the behaviour: no
provider, no OAuth flow, no webhook dispatcher.

The two rules that matter are on the models, because they are the ones that
get quietly broken later by somebody wiring a real provider under time
pressure:

**An integration is a service principal.** It carries its own `scopes` and
never inherits the connecting user's rights. `connected_by` is `SET_NULL`
provenance, so the integration survives that person leaving -- and, more
importantly, does not silently keep their rights if they are demoted.

**External data is evidence, never truth.** `ExternalObjectLink` records that a
pull request relates to a task. Nothing may read it to move the task. That one
is enforced by a test that greps the codebase and fails if anything outside
the model and its own tests references it -- so auto-completing a task from a
merged PR becomes a deliberate argument rather than a quiet commit.

`IntegrationCredential` is a separate table because it is the row that must
never be serialized, logged or returned; keeping it apart makes that boundary
visible rather than remembered. Its columns are named `encrypted_*` and there
is no encryption helper: writing one before there is a provider to use it
would be guessing at key management, and a half-built one that looks finished
is worse than an obviously empty seam.

`WebhookEvent` is unique on `(provider, external_event_id)` in the database
rather than in a handler that has to remember. Providers retry, and a retry
processed twice is how one merged pull request becomes two of something.

**19 tests** in `integrations/tests.py`.

---

## WP23 (part) — `docs/MONETIZATION_PLAN.md`

Written. It states the plans, how a limit resolves, how usage is counted, the
over-limit rules, what is deliberately not built, and the five steps to turn
enforcement on. The README half of WP23 is still outstanding.

---

## Test gate in use

Per commit: the affected app's tests, plus `makemigrations --check --dry-run`,
plus `ruff` on the touched files. The full suite runs at work-package
boundaries and before any PR.

### The suite was slow for one reason, and it has been fixed

Django 6.0 defaults to **1,200,000 PBKDF2 iterations**, which costs **2.6
seconds per password** on this machine. The suite creates a user for nearly
every actor in nearly every test -- six or seven per test in the task and
visibility modules -- so the great majority of its wall-clock time was spent
deliberately making hashes slow to compute. Nothing was being tested by that.

`conftest.py` now sets `PASSWORD_HASHERS` to MD5 in `pytest_configure`. Measured
on `projects_and_tasks/test_task_authority.py`, 26 tests:

| | Before | After |
|---|---|---|
| Fresh database | 223s | 76s |
| Reused database | — | **33s** |

Per-test cost went from ~8.6s to ~0.25s. The remaining per-module time is
database setup, not the tests.

It is set in `pytest_configure` rather than as an autouse fixture on purpose: a
function-scoped fixture runs *after* `setUpTestData`, and
`test_access_matrix.py` builds its whole world there -- it would have gone on
paying full price for the largest fixture in the suite.

Nothing asserts on the algorithm. Password *strength* is enforced by
`validate_password`, a separate mechanism, unaffected by this.

**Pre-existing baseline, recorded before any change on this branch:
511 passed, 1 failed.** The failure is
`api/tests.py::AIHealthSummarySecurityTests::test_rate_limit_boundary`, which
its own docstring says needs a real Redis at `CELERY_BROKER_URL`. It is an
environment failure, not a code failure, and not something this work introduced
or should fix.

**Corrected 2026-09-09.** The reason recorded here for months -- "Docker is not
running" -- was wrong. With every container up and `workroom-redis` healthy, the
test still fails: `docker-compose.yml` **publishes no host port for Redis**, so
it is reachable only as `redis:6379` inside the compose network and never from
a suite run on the host. Starting Docker does not and cannot fix it.

Two ways to make it pass, both a change to the dev environment rather than to
the code, and neither taken here: publish `6379:6379` in `docker-compose.yml`,
or run the suite inside `workroom-bd` (which would first need `pytest` --
the image installs `requirements.txt` only).

`ruff` also has **5 pre-existing errors** in
`projects_and_tasks/services.py` (2 × `I001`, 2 × `E501`) and
`users/services.py` (1 × `E501`). Left alone: fixing them would put unrelated
import-reordering noise into commits whose diffs are meant to be read closely.
Worth a separate `chore(lint)` commit at some point.

---

## DECISIONS NEEDING REVIEW

### R1 — RESOLVED: limits are built and measured, enforcement is off

**Decided 2026-09-09.** `ENTITLEMENTS_ENFORCED` defaults to `False`.

The conflict was three-way, not two:

- §11 of the V2 prompt: "real limits, real enforcement... not a document, not
  a permissive stub", with four seeded plans, an `Entitlements` service,
  `UsageCounter`, nineteen enforcement points, grace behaviour and six tests.
- The same prompt's OUT OF SCOPE list: "entitlement *enforcement* (limits must
  stay permissive)".
- `CLAUDE.md` §15 excludes an "advanced subscriptions/billing product" from
  V1; §16 says "do not expand billing into a V1 product feature".

Two of three said permissive, and `CLAUDE.md` is the repository's own standing
instruction rather than a one-off prompt. What settled it was building §11 as
written and running the suite: **46 failures**, because every company without a
subscription resolves to Free, and Free allows one department, zero teams and
no public projects. The product was built without limits, so switching them on
changes what existing customers can do on the day it ships.

Everything §11 specifies is built. Only the refusal is deferred, behind one
environment variable, and the usage data needed to decide the rollout is being
collected now. See `docs/MONETIZATION_PLAN.md` for the five steps to turn it
on.

### R2 — `docs/MONETIZATION_PLAN.md` is required but never specified

DEFINITION OF DONE requires `docs/MONETIZATION_PLAN.md`. No section describes
it; §11 is the billing content. Writing it as the prose rationale for §11 —
tier definitions, what is never paywalled, over-limit behaviour, grandfathering
— with §11's table as the normative numbers.

### R3 — Reference document path

The prompt cites `docs/workroom-v1-reference.md`. The file in the workspace is
`workroom-v1-reference.pdf`, one directory above the backend repo, and is not
committed to any repo. Using it in place; not copying a 180 KB PDF into the
repo without being asked.

### R5 — A manager can approve their own submitted work

Not introduced here, and not fixed here: `user_can_approve_task` resolves to
`task.created_by` (then `current_owner`, then the project's creator) and never
asks whether that person is the one who submitted. Anyone who can create a task,
assign it to themselves, and submit evidence can then approve it. After WP7b
puts creation behind MANAGE the path narrows to managers, but it does not close.

It is left alone because the obvious fix has a dead end in it. "The submitter
may not decide their own submission" is right until a project has exactly one
manager who is also doing the work -- then the task can never reach Done by any
route, which is the same trap WP5 removed from late submission.

The version that works is "refuse self-approval when another eligible approver
exists, and record it when one does not", which is a real decision about
separation of duties rather than a tidy-up, and it needs its own baseline
regeneration. Worth doing as WP7c or folding into WP10, where the fallback
chain gets revisited anyway.

### R6 — `today` mode is a daily focus list, not a backlog

§9's eligibility rule ("deadline is inside the window or already overdue")
combined with its window rule ("`today` mode = today only") means an AI to-do
generation in `today` mode draws only on tasks due today or already overdue.

Implemented literally, because §9 says it twice -- the second time as "only
exclude completed, archived, and out-of-window-future work". But it is worth a
conscious look, because the product consequence is not small: a person with a
full fortnight of scheduled work and nothing due today gets an empty result.
The message says so precisely rather than claiming they have no tasks, so it is
honest either way.

If the intent was "everything open, dated into today", the change is one
argument in `eligible_tasks_for_generation`.

### R4 — Three repositories, one branch name

The prompt assumes a single repository. The workspace holds three:
`workroom-backend`, `workroom-frontend-main`, `workroom-ai`. Creating
`feat/v2-foundation` in each repo as its work begins. The standing convention
of putting fix work on `hot-fix` is being set aside here because the prompt
names the branch explicitly.

`workroom-frontend-main` has 39 uncommitted files on its `hot-fix` branch,
predating this work. Not touching them; frontend work starts from a clean tree
or from an explicit instruction about what to do with those changes.
