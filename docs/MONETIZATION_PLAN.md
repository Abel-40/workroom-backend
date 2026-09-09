# Monetization plan

What the plans are, what enforces them, and what is deliberately not switched
on yet.

---

## The one thing to know first

**Plan limits are built, measured, and not enforced.** `ENTITLEMENTS_ENFORCED`
defaults to `False`.

Every check runs. Usage is counted and reconciled. Every call reports the true
numbers, so a UI can already say "5 of 3 projects used" and offer an upgrade.
What is switched off is only the refusal.

This is not a stub. It is a deliberate default, and the reasoning is in
[R1](BUILD_LOG.md#r1--11-and-out-of-scope-contradict-each-other-on-entitlement-enforcement).
The short version: three source documents disagree, two of the three say
limits stay permissive in V1, and turning enforcement on makes the
disagreement concrete — **46 existing tests fail**, because every company
without a subscription resolves to Free, and Free allows one department, zero
teams and no public projects. The product was built without limits. Applying
them is a decision with real customer consequences, not a default.

Turning it on is one environment variable. The data needed to decide when is
already being collected, which is the part that cannot be reconstructed later.

---

## The four plans

Seeded by `plans/migrations/0003_seed_plan_limits.py`. `null` means unlimited
throughout.

| | Free | Team | Business | Enterprise |
|---|---|---|---|---|
| Members | 5 | 50 | ∞ | ∞ |
| Active projects | 3 | ∞ | ∞ | ∞ |
| Departments / teams | 1 / 0 | ∞ / ∞ | ∞ / ∞ | ∞ / ∞ |
| Storage | 2 GB | 100 GB | 1 TB | custom |
| Info Portal pages | 50 | ∞ | ∞ | ∞ |
| AI credits | 50 flat | 300/user | 1000/user | custom |
| AI model tier | economy | standard | premium | premium |
| AI planning / month | **0** | 5 | ∞ | ∞ |
| Integrations | 0 | 0 | ∞ | ∞ |

Only **Active** projects count. A company that archives or finishes work is not
punished for having done it.

`0` and `null` are different and the difference matters most for AI planning:
Free is `0`, meaning *disabled*, and Business is `null`, meaning *unlimited*.
Confusing them would give the free tier unlimited access to the single most
expensive call in the product.

### Features by plan

| Feature | Free | Team | Business | Enterprise |
|---|---|---|---|---|
| `workload_policy_warn` | | ✓ | ✓ | ✓ |
| `workload_policy_block` | | | ✓ | ✓ |
| `skills_matching` | | ✓ | ✓ | ✓ |
| `department_analytics` | | ✓ | ✓ | ✓ |
| `company_analytics` | | | ✓ | ✓ |
| `audit_log_view` | | ✓ | ✓ | ✓ |
| `audit_log_export` | | | ✓ | ✓ |
| `public_projects` | | ✓ | ✓ | ✓ |
| `custom_roles` | | | ✓ | ✓ |
| `integrations` | | | ✓ | ✓ |
| `sso` | | | | reserved, unimplemented |

Personal and project-aggregate analytics are **never** gated. A product that
hides your own workload behind an upsell is a worse product.

---

## How a limit is resolved

```
company
  └─ subscription.plan_snapshot   ← read first
       └─ subscription.plan       ← fallback for legacy rows
            └─ the seeded `free` plan  ← fallback for no subscription at all
```

**`plan_snapshot` is the important one.** It freezes the limits at subscription
time, so editing `team`'s numbers never silently changes what an existing
customer is already paying for. An FK alone cannot express that — it follows
the row wherever it goes. A test pins it.

A company with no subscription resolves to Free. That is a real, common state
— every company registered before billing existed is in it — and treating it as
"no limits" would apply the limits to paying customers and nobody else.

---

## How usage is counted

**Counters are for speed; rows are for truth**, and the decision path takes the
truth.

- **Point-in-time metrics** (members, active projects, departments, teams,
  storage, pages, integrations) are counted **from the rows** at check time.
  §11 describes checking against an incremented counter, which works right up
  until one create path forgets to increment — and then the limit is silently
  wrong in whichever direction the bug went. Reading rows cannot drift from
  them by construction.
- **Monthly metrics** (AI credits, planning generations) use the counter,
  because spending leaves no other trace: the counter *is* the record. They
  reset by period key — a new month is a new row starting at zero, so there is
  no job to fail at midnight on the first, and last month stays readable.

A nightly job (`entitlements.tasks`) recomputes the point-in-time counters and
logs every correction. Nothing depends on it for correctness; its value is
making drift *visible*, because a counter that needs correcting regularly means
a create or delete path is not recording usage.

### AI credit costs

| Operation | Credits |
|---|---|
| Assistant question | 1 |
| To-do generation | 2 |
| Health summary | 3 |
| Planning generation | 15–25 |

Planning scales with brief size: 15, plus one per 2000 characters, capped at
25. The cap stops a pathologically long brief emptying a month's pool in one
call. Every AI check happens **before** the provider call, so a refusal never
costs a request.

---

## Over-limit behaviour

Soft-block, never destructive. This is not negotiable and is the part most
likely to be got wrong under pressure.

- Hitting a limit blocks **the new thing only**. Existing data stays fully
  readable and usable.
- A company at 6/5 members after a downgrade **keeps all six**. Nobody is
  removed, nothing is archived. They cannot add a seventh.
- `past_due` keeps full access for **14 days**, then goes read-only for new
  creation. Existing data remains readable throughout. The clock runs from
  `past_due_since` rather than `updated_at`, because a grace period that any
  unrelated write silently restarts is not a grace period.
- `past_due` with no recorded start is treated as **in grace**. Guessing
  against the customer on missing data is how a billing bug becomes a support
  incident.
- **Data is never deleted for non-payment.** Ever.

Every refusal is a `402` naming the limit and the current number, never a bare
`403`. "You are at 5 of 5 members on Free" is actionable; "denied" is not.

---

## What is not built

- **No Stripe expansion.** `CLAUDE.md` §16 is explicit that billing is not a V1
  product feature, and nothing here touches checkout, webhooks or invoices.
  The existing checkout code is unchanged.
- **No plan-change flow.** Nothing in the product moves a company between
  plans; that is done in the admin. Writing a self-serve upgrade path before
  deciding whether limits are enforced would be building the checkout for a
  door that is currently open.
- **No proration, dunning, or invoice handling.**
- **No usage-based billing.** Credits are a limit, not a meter that bills.

---

## Turning enforcement on

1. Decide it, and record the decision against R1 in `BUILD_LOG.md`.
2. Look at the real numbers first — they are already being collected. A query
   over `UsageCounter` and the row-count helpers in `entitlements.services`
   answers "how many existing companies would be over a limit tomorrow".
3. Set `ENTITLEMENTS_ENFORCED=true`.
4. Expect the 46 tests noted above to need their fixtures moved onto a plan
   that permits what they do; they are fixtures, not behaviour.
5. Ship the upgrade prompts before the refusals reach anyone. A `402` with no
   route to fix it is worse than no limit.
