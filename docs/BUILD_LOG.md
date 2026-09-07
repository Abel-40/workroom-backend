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
| WP2 | `AuditEvent` + `record_event()` (§10) | |
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
