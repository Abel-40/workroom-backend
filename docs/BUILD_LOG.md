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
| WP3 | `resolve_project_access`, `ProjectMembership`, collaborator migration (§10) | |
| WP4 | Company context returns a membership; Owner backfill (§10) | |
| WP5 | Deadlines: `<=`, AI buffer removal, MANAGE + reason + audit; late submission (§2) | |
| WP6 | `ApprovalRequest`; visibility escalation and the public gate (§1, §10) | |
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
| WP19 | `docs/DECISIONS.md` (§13) | |
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
