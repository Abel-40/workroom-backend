# Permission inventory

Every place the codebase decides "may this user do this," as of the start of
the V2 foundation work. This is the worklist for the access-model
consolidation: when `resolve_project_access` lands, every project-scoped entry
below becomes a call to it, and this document becomes the checklist proving
none were missed.

Companion artifacts:

- `crm_backend/projects_and_tasks/test_access_matrix.py` — characterization
  suite; the generated matrix is the proof that a refactor changed nothing.
- `crm_backend/projects_and_tasks/access_matrix_baseline.txt` — 960 recorded
  answers across the full role × visibility × relationship cross-product.

---

## 1. Company context resolution

`crm_backend/company/services.py` — the root of every check below. Each
function re-derives the company from server-side state and never reads a
client-supplied id.

| Function | Answers | Notes |
|---|---|---|
| `get_owned_company(user)` | the company this user owns | `Company.owner` is the authority on ownership |
| `get_managed_company(user)` | owned company, else first active CM/DL membership | admin-scoped actions |
| `get_member_company(user)` | owned company, else first active membership of any role | baseline "may act in this company" |
| `is_company_member(user, company)` | whether an **arbitrary target** belongs to a company | used to validate assignees/collaborators/leaders |
| `get_company_role(user, company)` | `Owner` if they own it, else active profile role, else `None` | |
| `get_member_department_id(user, company)` | their department, `None` for the owner | |

Each has a `_sync` mirror for use inside a transaction (Django transactions are
sync-only). The async and sync variants must stay behaviourally identical —
they are currently duplicated logic, not one implementation with two entry
points.

**Two structural problems, both scheduled:**

1. Every one of these assumes the user belongs to **exactly one company** —
   they take a user and return *the* company. A second membership is silently
   unreachable. Fixed by having context resolution return the
   `CompanyUserProfile` and accept an explicit company identifier.
2. `Owner` is resolved from `Company.owner`, not from a membership row, so the
   owner may have **no `CompanyUserProfile` at all**. Registration creates one
   today (`api/api.py:260`), but legacy rows exist, and several modules still
   carry an "owner might have no profile" branch. Fixed by a backfill migration
   plus deletion of every such branch.

### Call sites by module

| Module | Count | What it gates |
|---|---|---|
| `users/services.py` | 33 | invitations, member roles, deactivation, removal |
| `projects_and_tasks/services.py` | 21 | everything project- and task-scoped |
| `pages/services.py` | 11 | Info Portal folders and pages |
| `departments_and_teams/services.py` | 11 | department/team CRUD and leadership |
| `api/api.py` | 8 | registration, invite acceptance, profile |
| `event_management/services.py` | 6 | events and event types |
| `api/routers/company_config.py` | 5 | company settings |
| `api/routers/pages.py` | 4 | page endpoints |
| `api/routers/event_types.py` | 4 | event-type endpoints |
| `api/routers/analytics.py` | 4 | analytics endpoints |
| `api/routers/todos.py` | 3 | personal to-dos |
| `api/routers/activity.py` | 3 | activity feed |
| `permissions/services.py` | 2 | the YAML catalog bridge |
| `api/routers/{teams,tasks,task_types,departments}.py` | 2 each | listing endpoints |
| `ai_agent/services.py` | 2 | generation requests |

---

## 2. The project/task predicates

`crm_backend/projects_and_tasks/services.py`. These eight functions are what
`resolve_project_access` replaces. Every one is recorded in the
characterization matrix.

| Predicate | Line | Grants to |
|---|---|---|
| `user_can_view_project` | 65 | anyone if `public`; `created_by`; Owner/CM; anyone if `company`; matching-department member if `department`; collaborators if `private` |
| `user_can_manage_project` | 87 | `created_by`, `current_owner`, Owner/CM, DL of the project's own department |
| `user_can_manage_task` | 101 | `task.created_by`, else whoever can manage the project |
| `user_can_update_task_status` | 107 | the assignee only |
| `user_can_log_time` | 115 | the assignee, else whoever can manage the task |
| `user_can_delete_time_log` | 125 | the log's author, else whoever can manage the task. Takes `task` explicitly rather than reaching it through `log.task`: every caller already holds it with `project__company` selected, and traversing the FK inside an async context is a runtime error, not a slow query |
| `user_can_approve_task` | 131 | `task.created_by`; if null, `project.current_owner`; if null, `project.created_by` |
| `user_can_extend_deadline` | 144 | `project.created_by` **only** |

### Where they are called

| Call site | Predicate | Guards |
|---|---|---|
| `services.py:184` | view | project detail |
| `services.py:273` | manage_project | `update_project` |
| `services.py:460` | manage_project | `archive_project` |
| `services.py:472` | manage_project | `transfer_project_ownership` |
| `services.py:498,509,525` | manage_project | project image set/upload/remove |
| `services.py:544` | view | `get_viewable_project` — scopes task and document endpoints |
| `services.py:558` | view **or assignee** | `get_task_for_user` |
| `services.py:586` | view | **`create_task`** — see defect D1 |
| `services.py:629` | manage_task | `update_task` |
| `services.py:709` | manage_task | `assign_task` |
| `services.py:735` | update_task_status | `update_task_status` |
| `services.py:747` | manage_task | `archive_task` |
| `services.py:764` | log_time | `create_time_log` |
| `services.py:780` | delete_time_log | `delete_time_log` |
| `services.py:892,920` | approve_task | `approve_task`, `reject_task_approval` |
| `services.py:950,967` | extend_deadline | `extend_task_deadline`, `extend_project_deadline` |
| `services.py:1119` | view | `upload_document` |
| `services.py:1147` | view | `get_document_for_user` |
| `services.py:1153` | uploader **or** manage_project | `delete_document` |
| `ai_agent/services.py:26` | view | request a generation |
| `ai_agent/services.py:81` | view | read a generation |
| `ai_agent/services.py:90` | requester **or** manage_project | act on a generation |
| `ai_agent/assistant_services.py:20` | view | ask the assistant |
| `ai_agent/assistant_services.py:53` | view | **read a conversation** — see defect D8 |
| `ai_agent/assistant_services.py:62` | requester **or** manage_project | **delete a conversation** — see defect D8 |
| `ai_agent/health_services.py:19,39` | view | request/read a health summary |
| `api/routers/ai.py:150,208,255` | requester **or** manage_project | generation review endpoints |
| `api/routers/ai.py:283` | manage_task | apply an AI suggestion to a task |

### Gates that are not predicates

These live inline and are therefore invisible to the generated matrix. Each has
a named characterization test.

| Location | Rule |
|---|---|
| `update_project` (`services.py:275`) | DL/DM may never change `department_id` — `department_locked` |
| ~~`update_project` (`services.py:281`)~~ | ~~DM may never change `visibility`, even downward — `visibility_locked`~~ **Replaced in WP6** by `can_set_visibility`, which is a named function rather than an inline gate: `private` ↔ `department` needs only MANAGE (and a department), `company` needs a DL of that department at minimum, `public` needs Owner/CM plus `Company.allow_public_projects`. |
| `update_project` (`services.py:295`) | only `created_by` may reopen a `Done` project — `forbidden_revert` |
| `create_project` (`services.py:246`) | DL/DM are locked to their own department |
| `create_project` (`services.py:251`) | a DM's project is forced to `private` regardless of the requested visibility |
| `create_project` / `can_set_visibility` (WP6) | `public` needs `Company.allow_public_projects` **and** Owner/CM; `company` needs a DL of that department at minimum; `department` needs the project to have one — `public_projects_disabled`, `visibility_locked`, `department_required` |
| `update_company_settings` (`company/services.py`) | Owner only — resolved through `get_owned_company`, deliberately narrower than `get_managed_company` |
| `create_task` (`services.py:589`) | no new tasks on a `Done` project |
| `create_task`/`assign_task` (`services.py:611,724`) | DL/DM assignment is restricted to `list_eligible_assignees` |
| `submit_task_for_approval` (`services.py:829`) | assignee only, `In Progress` only, no pending approval, evidence required, **deadline not passed** |
| `list_projects_for_user` (`services.py:156`) | queryset-level visibility filter that **disagrees** with `user_can_view_project` — see defect D9 |

---

## 3. Inline role comparisons

Direct `CompanyUserProfile.Role.*` comparisons outside `company/services.py`.
These are what "no inline role comparisons remain outside the resolver" is
measured against.

| Module | Count | Notes |
|---|---|---|
| `projects_and_tasks/services.py` | 12 | all absorbed by `resolve_project_access` |
| `users/services.py` | 9 | member management — legitimately role-based (ADMINISTER tier), stays |
| `company/services.py` | 4 | the resolver itself |
| `api/api.py` | 3 | registration and invite acceptance |
| `users/models.py` | 2 | the `Role` choices definition |
| `event_management/services.py` | 2 | mirrors `user_can_manage_project` for events (`services.py:88-90`) |
| `departments_and_teams/services.py` | 1 | DL-only department action |
| `analytics/services.py` | 1 | synthesises an `Owner` role for the roster, because the owner may have no profile row |
| `api/routers/projects.py` | 1 | same synthesis |

The `analytics` and `api/routers/projects` entries are the Owner-without-a-
profile special case, and disappear with the backfill.

---

## 4. Frontend enforcement

The frontend is already consolidated — this is better than the plan assumed.
There is no scatter of inline role strings in components to clean up; the work
is extending what exists to three access levels.

| File | Role |
|---|---|
| `src/lib/permissions.ts` | flat role → permission-code catalog, mirroring `permissions/catalog.py` and `roles_permission.yaml` |
| `src/lib/projectPermissions.ts` | `canManageProject` / `canManageTask`, mirroring the backend predicates |
| `src/lib/eventPermissions.ts` | `canManageEvent` |
| `src/composables/usePermissions.ts` | the single composable components consume |

`usePermissions()` already wraps all three, and components are expected to go
through it rather than reading `authStore.logedInUserInfo.role`. Adding
`useProjectAccess(project)` means replacing `canManageProject`/`canManageTask`
with a `VIEW | CONTRIBUTE | MANAGE` resolver mirroring the backend one.

### FRONTEND-ONLY ENFORCEMENT

Rules the UI applies that the server does not. Each is a security bug: the
endpoint accepts the request if called directly.

| Rule | Where | Server position |
|---|---|---|
| **Task creation is offered only to roles holding `tasks:create`** | `permissions.ts` catalog | The server gates `create_task` on *project view* alone (`services.py:586`). Every role holds `tasks:create` in the catalog, so today the two happen to agree — but the catalog is the only thing expressing "who may create a task", and it is not consulted by `create_task`. Closed when task creation moves behind MANAGE. |
| **`manage_any` tiering for projects/tasks/events/documents** | `isCompanyAdmin()` | The server has no `manage_any` concept; it re-derives Owner/CM inside each predicate. Consistent in effect, duplicated in fact. Closed by the resolver. |
| **Member-row locking** (`isMemberRowLocked`) | `permissions.ts` | Genuinely mirrored server-side in `users/services.py`. **Not a bug** — listed for completeness. |
| **Department scoping for DL actions** | `projectPermissions.ts` | Mirrored server-side. **Not a bug.** |

The catalog itself (`permissions/catalog.py`, loaded from
`permissions and roles/roles_permission.yaml`) is consulted by
`permissions/services.py:user_has_permission`, which has **two call sites**.
The catalog is therefore close to advisory: the real enforcement is the
predicates above. Preserved as-is per the brief, but worth stating plainly —
the YAML is not what stops anyone from doing anything today.

---

## 5. Defects recorded by the characterization suite

Each has a pinned test in `test_access_matrix.py` and a decision that changes
it. None are fixed by the extraction itself.

| # | Defect | Decision |
|---|---|---|
| D1 | ~~Project *view* is all it takes to create a task, so `company` visibility — meant to be discovery only — lets any member add work to any project~~ | **FIXED in WP7b.** `create_task` requires MANAGE. Contributors get `POST /projects/{id}/task-proposals/` → `ApprovalRequest(kind="task_proposal")`, which someone with MANAGE turns into a real task *through `create_task`* — never by writing the payload into the tasks table. The two landed in one commit on purpose: tightening creation without the replacement would have left contributors unable to raise work at all. |
| D2 | ~~`user_can_manage_task` short-circuits on `task.created_by`, so a creator keeps edit/assign/archive rights over a task on a project they cannot otherwise touch~~ | **FIXED in WP7a.** `user_can_manage_task` is now exactly `user_can_manage_project`. The baseline priced it first: **240 of 1440 combinations** granted management of a task on a project the same person could not open, all of them DL (96) or DM (144). The regenerated baseline moved 240 rows in one column (`own_task`), one direction (1 → 0), nothing else — predicted and actual blast radius matched. `created_by` is provenance, as on `Project`; continuing authority comes from a manager `ProjectMembership`, which is visible and revocable. |
| D3 | Deadline changes are `created_by`-only; the current owner, Owner, CM and the department's leader are all locked out, and a departed creator freezes the deadline forever | §2: MANAGE + required reason + audit |
| D4 | Deadline changes are extend-only | §2: shortening allowed unless it breaks the task invariant |
| D5 | `task.deadline` must fall **strictly** before `project.deadline` — the reason the AI generator carries a one-hour buffer hack | §2: `<=`, buffer deleted |
| D6 | A task cannot be submitted for approval once its deadline has passed, so a late task can never reach Done | §2: allow late, flag with `submitted_late`/`late_by` |
| D7 | `public` visibility short-circuits the company check, so any authenticated user of any tenant can read the project by id — and ~~any manager, including a DM managing their own project, can set it~~ | **FIXED in WP6** (the setting half). `can_set_visibility` gates `public` on `Company.allow_public_projects` *and* Owner/CM, from both the create and the update path, and audits the change. The reading half is not a defect and is left: `public` short-circuiting the company check is what `public` means. Existing public projects stay public — the gate applies to new transitions, and migration 0014 logs the affected rows for review. |
| D8 | Anyone who can view a project can read — and a project manager can delete — another user's AI Assistant conversation | §5: private by default, explicit sharing, delete-others endpoint removed |
| D9 | `list_projects_for_user` and `user_can_view_project` disagree: a creator sees a project by id that never appears in their list | §10: one resolver, one queryset derived from it |
| D10 | ~~Neither `user_can_manage_task` (on `created_by`) nor `user_can_manage_project` (on `current_owner`) performs any company check before granting rights~~ | **FIXED in WP3a.** Every predicate resolves membership before consulting any per-project reference. The case that made it more than theoretical: removal from a company leaves `created_by` intact as provenance, so a removed member kept view over projects they had created. `public` is still checked before membership, deliberately. |
| D11 | 30 combinations grant MANAGE without VIEW — a DL or DM can edit, archive and transfer a private project that `GET` returns 403 for | §10: an ordered `AccessLevel` cannot represent this; absorbed by `resolve_project_access` in WP3 |
