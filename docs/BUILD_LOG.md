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
| WP7 | Task field-level authority; task proposals; break-into-steps (§2) | |
| WP8 | Task dependencies — `blocks` / `relates_to` (§2) | |
| WP9 | `ProjectBrief` and brief-assisted creation (§1) | |
| WP10 | Skills, professions, capacity, workload, `AssignmentPolicy` (§3) | |
| WP11 | AI pipeline: allow-list serializer, re-validation, token accounting (§4) | |
| WP12 | AI assistant privacy; health-summary anonymity (§5) | |
| WP13 | Events: audience, attendees, RRULE (§6) | |
| WP14 | Unified `Document` model with scopes (§7) | |
| WP15 | Analytics tiers (§8) | |
| WP16 | To-dos: timezone, eligibility, supersede semantics (§9) | |
| WP17 | Plans, `Entitlements`, `UsageCounter`, enforcement (§11) | |
| WP18 | Integration seams (§12) | |
| WP19 | `docs/DECISIONS.md` (§13) | **done** |
| WP20 | Frontend consolidation: `useProjectAccess`, regenerated types (§ throughout) | |

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
   lands.

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

## Test gate in use

The full suite takes **41 minutes** on this machine, which is not a workable
per-commit gate. Per commit: the affected app's tests, plus
`makemigrations --check --dry-run`, plus `ruff` on the touched files. The full
suite runs at work-package boundaries and before any PR.

**Pre-existing baseline, recorded before any change on this branch:
511 passed, 1 failed.** The failure is
`api/tests.py::AIHealthSummarySecurityTests::test_rate_limit_boundary`, which
its own docstring says needs a real Redis at `CELERY_BROKER_URL`; Docker is not
running on this machine. It is an environment failure, not a code failure, and
not something this work introduced or should fix.

`ruff` also has **5 pre-existing errors** in
`projects_and_tasks/services.py` (2 × `I001`, 2 × `E501`) and
`users/services.py` (1 × `E501`). Left alone: fixing them would put unrelated
import-reordering noise into commits whose diffs are meant to be read closely.
Worth a separate `chore(lint)` commit at some point.

---

## DECISIONS NEEDING REVIEW

### R1 — §11 and OUT OF SCOPE contradict each other on entitlement enforcement

§11 is titled "real limits, real enforcement" and says, in bold, "Implement
actual plan limits now. Not a document, not a permissive stub." It then
specifies four seeded plans, an `Entitlements` service, a `UsageCounter` model,
nineteen enforcement points, over-limit and grace-period behaviour, and six
tests.

OUT OF SCOPE lists "entitlement *enforcement* (limits must stay permissive)".

These cannot both hold. Reading it as a leftover from the earlier draft of the
prompt (which asked for a documented plan plus an always-yes stub), and
building §11 as written, because §11 is specific, detailed, and internally
consistent, whereas the OUT OF SCOPE line is a single clause that would render
an entire numbered section dead. **Confirm before WP17.**

The cost of being wrong in this direction is real: enforcement changes what
existing customers can do the moment it ships. If the permissive stub was
intended, say so and WP17 shrinks to `MONETIZATION_PLAN.md` plus the seam.

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

### R4 — Three repositories, one branch name

The prompt assumes a single repository. The workspace holds three:
`workroom-backend`, `workroom-frontend-main`, `workroom-ai`. Creating
`feat/v2-foundation` in each repo as its work begins. The standing convention
of putting fix work on `hot-fix` is being set aside here because the prompt
names the branch explicitly.

`workroom-frontend-main` has 39 uncommitted files on its `hot-fix` branch,
predating this work. Not touching them; frontend work starts from a clean tree
or from an explicit instruction about what to do with those changes.
