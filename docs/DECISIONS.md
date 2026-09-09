# Decisions that constrain future work

Things settled now so that later work does not quietly foreclose them. **None
of this is built, and none of it should be built** until it is asked for. The
point of writing it down is that each item is cheap to preserve and expensive
to retrofit — every one of them is a decision you only get to make once, at the
moment somebody touches the model in question.

Each entry says what the constraint is, why it exists, and what would violate
it.

---

## 1. A person and a membership are different things

`User` is the person. `CompanyUserProfile` is that person's membership of one
company. The split stays exactly as it is.

**Why.** It is the only reason multi-company membership remains reachable at
all. Collapse them — put a role, a department or a capacity on `User` — and one
person can never belong to two companies without contradicting themselves.

**What violates it.** Any company-scoped field landing on `User`. Role,
department, team, capacity, availability, employment type, job title,
company-specific contact details, notification preferences. All of these belong
on the membership.

**The existing exceptions are deliberate and should stay rare.** `User.timezone`
and `User.theme` are personal, not company-scoped — the same person wants the
same clock and the same colours everywhere. `welcome_email_sent` is a
per-account fact. That is the whole list. A new field on `User` needs the same
argument: *this is true of the human regardless of who they work for.*

---

## 2. Capacity and availability live on the membership

Weekly capacity, working hours, availability and employment type attach to
`CompanyUserProfile`, never `User`.

**Why.** Someone three days a week at one company and two at another is one
person with two capacities. A single number on `User` cannot represent that,
and the workload figures computed from it would be wrong for both employers.

**What violates it.** A `weekly_capacity_hours` on `User`, or any workload
computation that sums across companies.

---

## 3. Notifications key off `(user, company)`

`Notification` currently keys off `recipient` alone, with no company column.

**Do not restructure the model for this now.** But the day it is touched for
any reason, add the company. Otherwise a person in two companies gets one
undifferentiated stream, cannot mute one employer without muting the other, and
— worse — a notification's title and body can leak the existence of company A's
projects into a session scoped to company B.

The same applies to `CompanyUserProfile.email_notifications_enabled`: it is
already per-membership, which is correct. Keep it that way.

---

## 4. Public content is a snapshot, never a flag on the live row

Whenever public content exists — a published project, a public profile, a
shared page — it is a **separate, explicitly whitelisted record** carrying only
the fields chosen for publication. It is never the internal row with a
`is_public` boolean on it.

**Why.** A visibility flag means every future query has to remember to filter,
every new field on the model is public by default the moment it is added, and
one missing `.filter()` anywhere is a data leak. A snapshot inverts that: new
fields are private by default, and publishing is an explicit act with an
explicit payload.

This is why the current `Project.visibility = "public"` is a gate on a
transition rather than a publication mechanism, and why the snapshot model that
would make it a real publication is deliberately not built yet. The gate is
honest about being a gate.

**What violates it.** Adding a public-facing read path that serialises an
internal model directly.

---

## 5. Nothing public is ever auto-derived from company data

No feature computes something publishable out of a company's own records
without a person deciding, per item, that it should be published.

**Why.** Aggregates leak. A public "projects completed this quarter" figure
reveals headcount, velocity and customer count; a public profile auto-filled
from a member's task history reveals what their employer is working on. The
person publishing has to be able to see exactly what they are publishing.

**What violates it.** Any derived public statistic, a profile populated from
internal activity, or a feed generated from company events.

---

## 6. An integration is a service principal, not a person

Whenever integrations are built: a connection holds its own scopes and never
inherits the rights of the user who connected it.

**Why.** Otherwise an integration silently becomes a privilege-escalation
route, and it keeps working after the person who authorised it has left.

**Related:** `ExternalIdentityLink` is per-company, user-initiated and verified.
A verified link between a Workroom account and an external identity in one
company must never carry across to another — that would let membership of one
company reveal membership of another.

---

## 7. External data is evidence, never truth

An external system can attach evidence to a Workroom record. It can never
change one. A linked pull request shows up on a task approval as evidence for a
human to weigh; it does not complete the task.

**Why.** The approval trail is the product's record of who decided what.
Something outside the tenant boundary must not be able to write into it, and a
misconfigured webhook must not be able to mark work done.

---

## 8. The AI proposes; a human commits

Restated here because everything above interacts with it. The AI never writes
domain state. Django builds the payload, the service proposes, Django
re-validates from scratch against its own rules, and a person commits the
result.

**What violates it.** Any path where a model response creates, assigns,
completes or approves something without a human step — including a
"convenience" auto-apply on a review screen.

---

## 9. Deliberately not decided

Recorded so nobody assumes silence means agreement:

- **Whether a person can hold two active memberships in the same company.** The
  `unique_together (company, user)` constraint says no. That is the current
  answer and it is fine; it just was not a considered decision.
- **What happens to audit rows when a company is deleted.** Currently they
  cascade, which is deliberate (see `audit/models.py`) but has not been checked
  against any retention obligation.
- **Whether `created_by` should survive the person being removed from the
  company.** It does, as immutable provenance. It no longer grants anything
  (see the membership check in `resolve_project_access`), so the remaining
  question is only whether the *name* should still be displayed.
