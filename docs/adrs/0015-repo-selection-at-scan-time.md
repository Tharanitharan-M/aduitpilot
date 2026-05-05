# ADR-0015: Repo Selection at Scan Time, Not After

**Date:** 2026-05-04
**Status:** Accepted
**Deciders:** AuditPilot maintainers
**Amends:** [ADR-0004](0004-read-only-by-design.md) (read-only-by-design — narrows the read surface further); [ADR-0008](0008-free-tier-infrastructure.md) (cost discipline — fewer repos scanned == fewer LLM tokens spent)
**Refs:** PLAN.md Sprint 3.5 (new), Sprint 4 (chunks 4.4a/4.4b), Sprint 9 (chunk 9.11); US-002, US-030; system-design §3.1, §6.1

---

## Context and Problem Statement

US-002 (Connect GitHub read-only) currently asks the user only to authorize the GitHub OAuth grant. After authorization, AuditPilot is implicitly scoped to **every repo in the connected GitHub organization**. The first version of PLAN.md Sprint 4 chunk 4.4 reflected this: the orchestrator calls GitHub MCP `list_repos` and then `get_branch_protection` against every repo it finds.

The "select which repos to scan" capability was deferred to PLAN.md Sprint 9 chunk 9.11, where US-030 ("Re-run a scan with different parameters") introduces a `repo_include_list` as a `params_override` field on the re-run flow.

Two real-world concerns surfaced during the Sprint 3 review:

1. **Multi-service organizations are the common case, not the exception.** A typical readiness customer has multiple GitHub repos representing different services. Only a subset is in scope for SOC 2 readiness — production services touching customer data, not internal tooling, marketing sites, research notebooks, or experimental code. Scanning everything by default produces:
   - Wasted LLM tokens and GitHub API quota on out-of-scope repos.
   - Spurious FAILING controls on repos that legitimately do not need branch protection (e.g. a personal sandbox), drowning the Pending Actions queue.
   - A worse trust narrative: "we read everything in your org" is harder to defend than "we read what you told us to read."

2. **Read-only-by-design (ADR-0004) is strengthened, not weakened, by user-controlled scope.** The architectural identity of the project is that Maya can verify our claim (read-only, no write APIs) without reading source. Adding a visible repo-picker step before any read happens is the same trust signal applied one level up: the user controls **which** reads happen, not just that reads are read-only.

The question: should `repo_include_list` be a first-class connector property set at connect time, or a `params_override` retrofit available only on re-runs?

---

## Decision

**Repo selection happens at scan time, persisted on the connector record. The first scan is gated on a non-empty selection. The orchestrator only fetches evidence from selected repos.**

Concretely:

1. **New table `connector_scoped_repos`** (Drizzle migration in Sprint 3.5):
   - Columns: `id`, `connector_id` (FK), `user_id` (FK), `provider_repo_id`, `full_name`, `private`, `selected_at`.
   - Composite unique index `(connector_id, provider_repo_id)`.
   - RLS policy by `user_id`.
2. **Repo-picker UI** between connect and first scan. shadcn `DataTable` (or `Command` palette for >100 repos) with checkbox selection, search, "select all in org `<X>`" shortcut, and a default of zero selected.
3. **`PATCH /api/connectors/{id}/scoped-repos`** endpoint accepting a typed `ScopedReposPatch { repos: ScopedRepoSelection[] }` body. Validates ownership, persists to the new table, idempotent.
4. **Orchestrator reads `connector_scoped_repos`** at the start of every scan run and refuses to start with an empty list (returns a typed `ScanRunValidationError` that the UI translates to "Pick at least one repo to scan").
5. **`POST /api/scan-runs`** still accepts `params_override.repo_include_list` for one-off overrides. When omitted, the persisted scope is the source of truth. US-030 re-run becomes "edit the scope on the connector and re-run" rather than "stash a bespoke include-list on the scan run."

This shifts the include-list from a Sprint 9 retrofit to a Sprint 3.5 / Sprint 4 first-class concept.

### What changes

- **PLAN.md Sprint 3.5 (new):** three chunks — migration, picker UI, PATCH endpoint.
- **PLAN.md Sprint 4 chunk 4.4:** split into 4.4a ("read scoped repos, refuse to start on empty") and 4.4b (parallel evidence fetch limited to scoped repos).
- **PLAN.md Sprint 9 chunk 9.11:** simplified to "edit-scope-and-re-run" UX rather than a separate include-list params override.
- **US-002:** new Gherkin clause for repo-picker step between OAuth authorization and first scan.
- **US-030:** simplified — re-run reuses the persisted scope; "different repo include-list" becomes "edit scope first, then re-run."
- **system-design §3.1, §6.1:** add the `connector_scoped_repos` table and the picker step in the connect-flow sequence diagram.
- **`connectors` table:** no schema change. The new join table carries the scope.

---

## Rationale

**1. Cost matters even on free tier.** ADR-0008 commits to $0/month. Every avoided LLM call on an out-of-scope repo extends that runway. On a 50-repo org with average 5 evidence pieces per repo, restricting scope to 8 in-scope repos cuts the worst-case scan cost by ~84% before any caching kicks in.

**2. Default-deny matches the project's stated identity.** Read-only-by-design (ADR-0004) reads better when the user also chose what was read. "We collect evidence from the eight repos you picked" beats "we crawl your entire org and trust the read-only scopes." The picker is the trust artifact.

**3. False-positive volume is the silent killer of compliance tooling.** US-007 (Pending Actions queue) is a triage UX. If half the cards are noise from out-of-scope repos, Maya's first reaction is to dismiss the queue, not act on it. Scope-at-connect-time is the cheapest input filter.

**4. Sprint 9 retrofit creates a confused re-run flow.** US-030 originally introduced `repo_include_list` as a one-off `params_override`. With the connector-level scope in place, US-030 becomes the natural "edit the scope, then re-run" UX, with one canonical place to look up "what was scanned." The retrofit would have left two sources of truth (persisted org-scope vs. ad-hoc params_override).

**5. The picker is cheap to build with shadcn.** `DataTable` with checkboxes, server-side fetch from GitHub MCP `list_repos`, persistence via the new endpoint. Roughly two days of work in Sprint 3.5, much smaller than the orchestrator wiring it gates.

---

## Consequences

### Positive

- LLM and GitHub API spend scales with chosen repo count, not org size.
- Pending Actions queue stays signal-dense from the first scan.
- The trust story now extends to read selection, not just read scopes.
- `params_override.repo_include_list` retains a use case (one-off "scan only this repo for a quick check") without being the primary mechanism.

### Negative

- Sprint 3.5 adds three days of work (migration + UI + endpoint) before Sprint 4's orchestrator-on-UI win.
- Adds one extra step to first-run UX. Mitigated by a "select all" shortcut; sophisticated users can bulk-include in seconds.
- `connector_scoped_repos` is one more RLS-protected table to maintain.

### Neutral

- The orchestrator's `list_repos` call is still useful — it populates the picker, just doesn't drive evidence collection directly.
- US-030 remains in scope for Sprint 9 but with a smaller surface (re-run reuses persisted scope; only when the user wants different scope they update the connector first).

---

## Alternatives Considered

### Alternative 1 — Status quo: scan all repos by default, retrofit include-list in Sprint 9

**Rejected.** This was the original plan. Rejected because:
- First-scan signal-to-noise ratio is poor.
- Trust narrative is weaker.
- LLM cost on multi-repo orgs is unbounded.
- A retrofit creates two sources of truth (persisted scope vs. params_override).

### Alternative 2 — Scope set on each `POST /api/scan-runs` instead of on the connector

**Rejected.** Forces the user to re-pick repos every scan. The connector-scoped-repos approach lets one selection drive every subsequent scheduled scan (Sprint 9 cron) without re-prompting.

### Alternative 3 — GitHub App with per-repo install instead of OAuth

**Tracked separately, not adopted now.** A GitHub App lets the user install AuditPilot on individual repos via GitHub's own UI, which would replace our picker with GitHub's. Tracked in the SYSTEM_DESIGN_RATIONALE.md backlog under "Sprint 5 extension — fine-grained PAT or GitHub App for private repo evidence." Not adopted today because (a) the OAuth flow is already shipped and Clerk-mediated, (b) building a GitHub App requires hosting an installation callback and a separate signing key, and (c) the picker UX is needed regardless to confirm the chosen scope.

---

## Migration

For an empty database (current state, pre-launch), Sprint 3.5 chunk 3.5.1 creates the table fresh.

For any existing customer with a connector but no scope (none today, but for clarity):
- On first dashboard load after deploy, the orchestrator detects an empty `connector_scoped_repos` and routes the user back to the picker before any scan can run.
- No silent default-to-all migration. Default-deny by design.

---

## Implementation Notes

- **Frontend:** `apps/web/app/dashboard/connectors/[id]/scope/page.tsx` is the picker route. shadcn `DataTable` for ≤ 100 repos, `Command` (cmdk) palette for larger orgs.
- **Backend:** `apps/api/routes/connectors.py` gains a `PATCH` handler. Reuses the existing IDOR ownership check pattern from the DELETE handler.
- **Schema:** `apps/api/db/migrations/0003_connector_scoped_repos.sql`. Drizzle definitions in `apps/api/db/schema.ts`.
- **Orchestrator:** the Sprint 4 graph reads `scope` as a state field at compile time, not at every step. `AuditOrchestrator` only sees the filtered list.

---

## References

- ADR-0004 — Read-only-by-design (this ADR is the user-controlled-read complement).
- ADR-0008 — Free-tier infrastructure (cost rationale).
- system-design §3.1 — Connector model.
- system-design §6.1 — Connect flow.
- US-002 — Connect GitHub read-only (updated with picker clause).
- US-030 — Re-run a scan run (simplified to reuse persisted scope).
- PLAN.md Sprint 3.5 — Connector scope (this ADR's implementation chunks).
