# AuditPilot — User Stories

**Status:** Draft | **Version:** 0.1 | **Date:** 2026-05-01
**Companion docs:** `docs/prd.md`, `docs/srs.md`, `docs/system-design.md`, `docs/adrs/`

> Stories are written from Maya's perspective unless explicitly noted. Maya is the founding-engineer persona from PRD §2.1: a first-or-second engineer at a 30-50 person B2B SaaS startup who needs SOC 2 readiness fast and has no compliance team. INVEST format throughout. Acceptance criteria use Gherkin GIVEN/WHEN/THEN. Implementation notes stay high level — `docs/system-design.md` is the source of truth for details.

---

## Story map (at a glance)

| ID | Title | Persona | JTBD | Sprint | PRD feature |
|---|---|---|---|---|---|
| US-001 | Sign up and sign in | Maya | JTBD-1 | Sprint 3 | Supabase Auth |
| US-002 | Connect GitHub read-only | Maya | JTBD-1 | Sprint 3 | GitHub read-only connector |
| US-003 | Run first readiness scan | Maya | JTBD-1 | Sprint 4 | Automated control evidence collection |
| US-004 | Disconnect a connector | Maya | JTBD-1 | Sprint 3 | GitHub read-only connector |
| US-005 | See connection status on dashboard | Maya | JTBD-1 | Sprint 3 | Dashboard surface |
| US-006 | View the 64-control posture grid | Maya | JTBD-1 | Sprint 4 | Control posture grid |
| US-007 | Triage Pending Actions queue | Maya | JTBD-7 | Sprint 4 | Pending Actions queue |
| US-008 | Inspect a Langfuse trace from a scan | Maya | JTBD-5 | Sprint 9 | Langfuse trace observability |
| US-009 | See published eval metrics | Maya | JTBD-5 | Sprint 10 | Promptfoo eval suite with judge validation |
| US-010 | Watch the orchestrator stream tool calls live | Maya | JTBD-1 | Sprint 4 | SSE streaming chat interface |
| US-011 | Draft an Incident Response Plan | Maya | JTBD-3 | Sprint 6 | Draft policy generation |
| US-012 | Edit a draft policy in the workspace | Maya | JTBD-3 | Sprint 6 | Draft policy generation |
| US-013 | Approve a policy via HITL gate | Maya | JTBD-3 | Sprint 6 | Human review gate |
| US-014 | Reject a policy with a reason and re-draft | Maya | JTBD-3 | Sprint 6 | Human review gate |
| US-015 | Download a policy as DOCX | Maya | JTBD-3 | Sprint 6 | Draft policy generation |
| US-016 | Upload a SIG-Lite XLSX for auto-fill | Maya | JTBD-2 | Sprint 7 | SIG-Lite questionnaire auto-fill |
| US-017 | Review flagged questionnaire cells | Maya | JTBD-2 | Sprint 7 | SIG-Lite questionnaire auto-fill |
| US-018 | Download the filled questionnaire XLSX | Maya | JTBD-2 | Sprint 7 | SIG-Lite questionnaire auto-fill |
| US-019 | Run an adversarial mock readiness challenge | Maya | JTBD-4 | Sprint 8 | Adversarial mock readiness challenge |
| US-020 | Download the gap report from a mock readiness challenge | Maya | JTBD-4 | Sprint 8 | Adversarial mock readiness challenge |
| US-021 | Run mock readiness challenge inside a self-hosted deployment | Security Lead | JTBD-4 | Sprint 8 + 11 | Adversarial mock readiness challenge |
| US-022 | Self-host AuditPilot inside our VPC | Security Lead | JTBD-6 | Sprint 11 | Docker Compose one-command local dev |
| US-023 | Fork an MCP server for a new domain | AI Engineer | JTBD-6 | Sprint 1 + 11 | Five published MCP servers |
| US-024 | Wire Promptfoo and judge validation into our CI | AI Engineer | JTBD-5 | Sprint 10 | Promptfoo eval suite with judge validation |
| US-025 | Receive a Pending Action when a control drifts | Maya | JTBD-7 | Sprint 9 | Drift watcher |
| US-026 | Dismiss a false-positive drift event | Maya | JTBD-7 | Sprint 9 | Drift watcher |
| US-027 | Draft an Access Control Policy | Maya | JTBD-3 | Sprint 6 | Draft policy generation |
| US-028 | Draft a Change Management Policy | Maya | JTBD-3 | Sprint 6 | Draft policy generation |
| US-029 | Draft a Vendor Management Policy | Maya | JTBD-3 | Sprint 6 | Draft policy generation |
| US-030 | Re-run a scan with different params | Maya | JTBD-1 | Sprint 9 | Re-run / compare / revert flows |
| US-031 | Compare two scan runs side by side | Maya | JTBD-1 | Sprint 9 | Re-run / compare / revert flows |
| US-032 | Revert a "Mark as done" action | Maya | JTBD-7 | Sprint 9 | Re-run / compare / revert flows |
| US-033 | Try the public demo without signing up | Casual reviewer | n/a | Sprint 11 | Public demo account (ADR-0012) |

---

## US-001: Sign up and sign in

**As** Maya, a founding engineer evaluating AuditPilot for the first time
**I want** to create an account and sign in with email or GitHub OAuth
**So that** I can start a readiness assessment without contacting sales or filling out a long form

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I am on the auditpilot.dev landing page and I have no account
WHEN I click "Sign up" and enter a valid email and password (≥ 8 chars)
THEN I receive a verification email within 60 seconds
AND clicking the verification link logs me into /dashboard

GIVEN I have an existing account and I am signed out
WHEN I click "Sign in with GitHub" and authorize the OAuth dialog
THEN I am redirected to /dashboard with an active session
AND my session cookie is HttpOnly + Secure + SameSite=Lax with a 7-day TTL

GIVEN I am signed in
WHEN I click "Log out" in the header
THEN my session is invalidated immediately
AND any subsequent request to /dashboard returns 302 to /login
```

### Why this matters

This is the entry door. JTBD-1 (PRD §3) requires that Maya can go from landing page to first scan in under five minutes. Friction at sign-up kills the funnel — a single extra form field correlates with a measurable drop in trial completion. Supabase Auth with both email and GitHub OAuth means Maya can sign in with whichever credential she has on hand. Choosing Supabase Auth over rolling our own means we delete approximately 800 lines of bespoke session-management code and inherit a battle-tested CSRF + session-rotation implementation. ADR-0008.

### Implementation notes (high level only)

- Frontend: `apps/web/app/(auth)/sign-up/page.tsx` and `apps/web/app/(auth)/sign-in/page.tsx`. Supabase Auth client (`@supabase/ssr`).
- Backend: `apps/api` verifies JWT on every authenticated request via `Depends(current_user)`.
- ADR-0008 (Supabase Auth selection); SRS FR-001 through FR-006.
- Data dependency: `users` table populated on first sign-in via Supabase webhook to `POST /api/internal/users/upsert`.

### PLAN.md chunks generated by this story

- [ ] Chunk 3.2 — Supabase Auth signup page
- [ ] Chunk 3.3 — Supabase Auth login page
- [ ] Chunk 3.4 — Logout button in header
- [ ] Chunk 3.5 — Protected `/dashboard` route with redirect

### Definition of done

- [ ] All three acceptance criteria pass in Playwright
- [ ] Auto test: Vitest unit test for form validation, Playwright E2E for full sign-up + verify + sign-in flow
- [ ] Manual test: sign up with a real email, verify, sign in, sign out
- [ ] OpenTelemetry span on `POST /api/internal/users/upsert` and `GET /api/me`
- [ ] Step Report produced; user committed

---

## US-002: Connect GitHub read-only

**As** Maya
**I want** to connect AuditPilot to my GitHub organization with read-only access
**So that** the orchestrator can collect SOC 2 evidence (branch protection, MFA, code scanning) without me granting any write permission

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I am signed in and have no GitHub connector
WHEN I click "Connect GitHub" on the onboarding page
THEN the GitHub OAuth dialog shows the literal scopes "repo:read" and "read:org"
AND no scope contains "write", "admin", or "delete"

GIVEN I have authorized the GitHub OAuth dialog
WHEN I am redirected back to /dashboard
THEN the connector card shows status "Connected"
AND a list of my GitHub repositories appears within 5 seconds

GIVEN GitHub returns an OAuth error (e.g. user denies access)
WHEN I am redirected back to /dashboard
THEN the connector card shows status "Not connected"
AND a clear, non-technical error message explains what happened

GIVEN I revoke the AuditPilot OAuth grant directly in GitHub Settings > Applications
WHEN AuditPilot's next read attempt fires
THEN the connector card flips to status "Error: re-authentication needed" within 30 seconds
AND no scan in progress is silently completed with stale data

GIVEN my GitHub account belongs to two organizations
WHEN I authorize the OAuth dialog
THEN the connector flow asks me which organization to connect
AND the chosen organization is recorded in `connectors.scopes_metadata.org_login`
```

### Why this matters

Read-only-by-design is the architectural identity of the project (ADR-0004). Showing the literal scope strings in the consent surface, before the GitHub dialog opens, is the trust signal that lets a security-conscious Maya verify our claim without reading our source code. Every other compliance tool either hides the scopes or buries them in a doc; we put them in the user's face. JTBD-1.

### Implementation notes (high level only)

- Supabase Auth handles the OAuth flow; the access token is stored server-side and never reaches the browser.
- Frontend: `apps/web/app/(dashboard)/connectors/page.tsx`. Reads connector status via `GET /api/me`.
- Backend: `POST /api/connectors/github`. ADR-0004, ADR-0008. SRS FR-003, FR-007, FR-014.
- Data dependency: `connectors` table.

### PLAN.md chunks generated by this story

- [ ] Chunk 3.6 — GitHub OAuth (read-only) connector via Supabase
- [ ] Chunk 3.7 — Display connected GitHub repos on dashboard

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest test verifying `POST /api/connectors/github` accepts only the read-only scope set; rejects any token with write scopes
- [ ] Manual test: real OAuth round-trip from a clean browser
- [ ] Sentry breadcrumbs scrub any `gho_*` token strings (verified by Sentry test event)
- [ ] OpenTelemetry span on `POST /api/connectors/github`
- [ ] Step Report produced; user committed

---

## US-003: Run first readiness scan

**As** Maya, having just connected GitHub
**I want** to click "Run readiness scan" and see the orchestrator collect evidence and map it to SOC 2 controls in real time
**So that** within one session I know which of 64 Trust Services Criteria controls I satisfy and which are gaps

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a connected GitHub connector and no prior scan_run
WHEN I click "Run readiness scan" on the dashboard
THEN an SSE connection opens to POST /chat with intent="run_readiness_scan"
AND tool-call cards (compliance-kb-mcp.search_controls, GitHub MCP get_branch_protection, etc.) stream in within 3 seconds
AND the 64-control posture grid populates progressively as control mapping completes

GIVEN a scan is running
WHEN the orchestrator finishes the control map
THEN a summary card shows total PASSING / FAILING / NOT_ASSESSED counts
AND the wall-clock duration is shown (target ≤ 30 seconds, NFR-001)

GIVEN a scan run completes
WHEN I look at the Pending Actions queue
THEN at least one Pending Action card exists for each FAILING control with a non-empty source_link

GIVEN a scan is in progress
WHEN I close my browser tab mid-stream
THEN the orchestrator detects the dropped SSE connection within 30 seconds via the `request.is_disconnected` channel
AND the running scan is gracefully cancelled (no further LLM calls billed)
AND a partial `scan_run` row is persisted with status="cancelled"

GIVEN a scan is in progress and one MCP tool returns a 502 Bad Gateway mid-run
WHEN the orchestrator handles the failure
THEN the failing tool call card renders with a red error state
AND the orchestrator continues with remaining MCP calls
AND the final control_map flags the affected controls as NOT_ASSESSED with reason="evidence collection failed"
```

### Why this matters

This is the headline flow. JTBD-1 in PRD §3. If first-time-to-control-grid is fast and the cards are clear, Maya understands the value within one session. If it is slow or unclear, she churns. The 30-second NFR-001 budget is the critical user-facing latency commitment — anything slower and the demo loses its impact.

### Implementation notes (high level only)

- Frontend: `useChat` hook from AI SDK 6 with `onToolCall` rendering tool cards. ADR-0003.
- Backend: AuditOrchestrator graph with parallel `asyncio.gather` over GitHub MCP tool calls + sequential `compliance-kb-mcp.search_controls` per evidence item. ADR-0001, ADR-0002.
- SRS FR-015 through FR-022.
- Data dependency: `scan_runs`, `evidence`, `control_map`, `actions` tables.
- Cache: content-hash cache on `(user_id, content_hash, control_id)` ensures re-scans are fast.

### PLAN.md chunks generated by this story

- [ ] Chunk 4.1 — AI SDK 6 `useChat` hook on `/dashboard` calls FastAPI `/chat`
- [ ] Chunk 4.3 — Wire compliance-kb-mcp via `MultiServerMCPClient`
- [ ] Chunk 4.4 — Add evidence collection step (GitHub MCP)
- [ ] Chunk 4.5 — Map evidence to controls

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: integration test with mocked GitHub MCP and mocked LLM verifies orchestrator state contains 64 control_map rows
- [ ] Manual test: connect a real GitHub org, click "Run readiness scan", verify within 30 seconds the grid populates
- [ ] Langfuse trace shows the full orchestrator span tree
- [ ] OpenTelemetry custom metric `orchestrator.scan.duration_ms` emitted to Grafana
- [ ] Step Report produced; user committed

---

## US-004: Disconnect a connector

**As** Maya
**I want** to disconnect any connector with one click and confirm
**So that** I can revoke AuditPilot's access whenever I want without contacting support

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a connected GitHub connector
WHEN I click "Disconnect" on the connector card and confirm in the modal
THEN the connector status changes to "Not connected" within 2 seconds
AND a DELETE call is made to GitHub OAuth to revoke our app authorization
AND the connector row is deleted from the database

GIVEN I have no connectors
WHEN I look at the dashboard
THEN no scan results render
AND the "Run readiness scan" button is disabled with tooltip explaining a connector is required

GIVEN I disconnect during a running scan
WHEN the orchestrator next attempts a GitHub MCP call
THEN it returns a clear "connector revoked" error
AND the scan is marked as cancelled with reason "connector revoked"
```

### Why this matters

Trust depends on revocability. ADR-0004 read-only-by-design is a strong claim, but only if the user can also cut access at any time. Disconnect flows that work poorly (or fail to actually revoke OAuth) erode trust faster than no disconnect at all.

### Implementation notes (high level only)

- Frontend: `apps/web/app/(dashboard)/connectors/page.tsx` Disconnect button.
- Backend: `DELETE /api/connectors/{connector_id}`. Calls Supabase Auth admin endpoint to revoke OAuth grant; deletes `connectors` row.
- ADR-0004. SRS not explicitly numbered; covered by intent of FR-007 + FR-014.

### PLAN.md chunks generated by this story

- [ ] Chunk 3.6.1 — Disconnect button on connector card
- [ ] Chunk 3.6.2 — `DELETE /api/connectors/{id}` endpoint with OAuth revocation

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies revoke is called on the Supabase Auth admin client
- [ ] Manual test: disconnect, then verify in GitHub Settings > Applications that AuditPilot is gone
- [ ] OpenTelemetry span on `DELETE /api/connectors/{id}`
- [ ] Step Report produced; user committed

---

## US-005: See connection status on dashboard

**As** Maya
**I want** an at-a-glance view on the dashboard of which connectors are healthy
**So that** I know whether my latest scan is using fresh evidence or stale data

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a healthy GitHub connector that was last used in the past hour
WHEN I open /dashboard
THEN the connector card shows status "Connected" in green
AND a "Last used" timestamp shows in human-friendly form ("just now", "5 minutes ago")

GIVEN the GitHub MCP server returned 401 on the last scan (token expired or revoked at GitHub)
WHEN I open /dashboard
THEN the connector card shows status "Error: re-authentication needed" in red
AND a "Re-connect" button is shown

GIVEN I have never connected any tool
WHEN I open /dashboard
THEN an empty state shows "Connect a tool to begin readiness assessment"
AND the connector card displays the available tools (GitHub primary; Gmail/Slack/Calendar marked Coming Soon)
```

### Why this matters

When evidence becomes stale, control posture decisions become stale. A user who sees "PASSING" for CC6.1 but does not realize the connector has been broken for 48 hours could miss a real drift. Connector health visibility is part of trust.

### Implementation notes (high level only)

- Frontend: `apps/web/app/(dashboard)/page.tsx` reads `GET /api/me` and renders a connector status panel.
- Backend: `connectors` table tracks `last_used_at` and `status`. The orchestrator updates these fields after every GitHub MCP call.
- ADR-0004, ADR-0008.

### PLAN.md chunks generated by this story

- [ ] Chunk 3.7.1 — Connector status panel on dashboard
- [ ] Chunk 3.7.2 — Update `last_used_at` and `status` after every MCP call

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Vitest snapshot test for the three states (connected / error / not connected)
- [ ] Manual test: revoke OAuth at GitHub, run scan, verify error state surfaces
- [ ] Step Report produced; user committed

---

## US-006: View the 64-control posture grid

**As** Maya
**I want** to see all 64 SOC 2 Trust Services Criteria controls as a green/yellow/red grid grouped by category
**So that** I can scan in 10 seconds which areas need the most work

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a completed scan_run with 64 control_map rows
WHEN I open /dashboard
THEN I see a grid grouped by category (CC1 through CC9)
AND each category header shows X/Y controls passing
AND each control cell is colored: green (PASSING), yellow (NOT_ASSESSED with confidence < 0.50), red (FAILING)

GIVEN I click on a single control cell (e.g. CC6.1)
WHEN the control detail panel opens
THEN it shows the control text, current status, confidence score, evidence_refs (clickable to evidence cards), and gap_description if FAILING

GIVEN I have no completed scan_run yet
WHEN I open /dashboard
THEN the grid shows all 64 controls as gray (NOT_ASSESSED)
AND a "Run your first readiness scan" CTA is prominent
```

### Why this matters

JTBD-1 ("know which controls I satisfy and which are gaps") is satisfied or not satisfied by this single screen. Maya does not want to read a 60-page report; she wants the eyeball test. If the grid is clear, the demo lands. If it is cluttered, the demo fails.

### Implementation notes (high level only)

- Frontend: `apps/web/app/(dashboard)/page.tsx` ControlPostureGrid component.
- Backend: `GET /api/scan-runs/{id}/control-map` returns the typed `ControlMapEntry[]`.
- ADR-0001, ADR-0005.
- The 64-control list is sourced from `compliance-kb-mcp` (CC1.1 through CC9.9).

### PLAN.md chunks generated by this story

- [ ] Chunk 4.6 — Render control posture grid on dashboard

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Vitest snapshot of the grid with a fixture of 64 control_map entries
- [ ] Manual test: complete a scan, eyeball the grid, click into 3 different controls
- [ ] Accessibility: each control cell has a text label in addition to color (color-blind safety)
- [ ] Step Report produced; user committed

---

## US-007: Triage Pending Actions queue

**As** Maya
**I want** a Pending Actions queue showing every gap as a card with a clear suggested fix and a direct link to the source tool
**So that** I can apply fixes one at a time without hunting through dashboards

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a completed scan_run produced 5 Pending Action cards
WHEN I open /dashboard
THEN the Pending Actions queue shows 5 cards
AND each card has a non-empty `what_changed`, `suggested_fix`, and `source_link`
AND clicking source_link opens the relevant GitHub settings page in a new tab

GIVEN I have applied a fix manually in the source tool
WHEN I click "Mark as done" on the card
THEN the card moves to "Completed" within the same session
AND the database records the timestamp and my user_id (FR-039)

GIVEN I do not want to apply a particular fix
WHEN I click "Reject" on the card and provide a reason
THEN the card moves to "Rejected" and is not shown on the next scan unless drift detection re-creates it
```

### Why this matters

This is the most novel surface in AuditPilot vs Vanta. Vanta tells you what is wrong; we draft the fix and link to the exact place to apply it. The 30-second magic moment from PRD §6 happens in this queue. JTBD-7.

### Implementation notes (high level only)

- Frontend: PendingActionsQueue component on `/dashboard`. Reads `GET /api/actions`. Mutates with `PATCH /api/actions/{id}`.
- Backend: actions table; ADR-0004; SRS FR-038, FR-039.
- The card payload includes a copyable draft (e.g. an access-review reminder email body) when applicable.

### PLAN.md chunks generated by this story

- [ ] Chunk 4.6.1 — Pending Actions queue UI
- [ ] Chunk 4.6.2 — `PATCH /api/actions/{id}` endpoint with state machine

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest state-machine test for action transitions (pending_review → approved/rejected/completed); Vitest component test for the queue
- [ ] Manual test: complete a scan with at least one FAILING control, apply the fix in GitHub, mark done in AuditPilot
- [ ] Step Report produced; user committed

---

## US-008: Inspect a Langfuse trace from a scan

**As** Maya, who is also an AI engineer evaluating the project as a reference architecture
**I want** to click a trace link on any scan_run and see the full Langfuse span tree
**So that** I can understand exactly how the orchestrator reasoned about each control

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a completed scan_run with a non-null langfuse_trace_id
WHEN I click "View trace" on the Mission Control panel
THEN a Langfuse trace URL opens with the full span tree
AND the trace includes spans for every MCP tool call and every LLM call

GIVEN I am on the Mission Control page
WHEN the dashboard loads
THEN the Langfuse trace iframe (or external link if iframe blocked) is visible for the most recent scan
AND total tokens, cost, and wall-clock duration are shown inline

GIVEN Langfuse Cloud is unreachable (free-tier outage)
WHEN I click "View trace"
THEN a friendly error explains the trace is temporarily unavailable
AND a fallback link to the local copy of trace metadata in the database is shown
```

### Why this matters

JTBD-5 ("trust AI quality without guessing"). For the AI engineer evaluating the project, the ability to inspect every span is the difference between "this is a real reference architecture" and "this is a marketing claim wrapped in code." We treat observability as a first-class user surface, not back-office tooling.

### Implementation notes (high level only)

- Frontend: `apps/web/app/mission-control/page.tsx` with embedded Langfuse iframe.
- Backend: `langfuse_traces` table links scan_run to trace URL.
- ADR-0009. SRS NFR-011.

### PLAN.md chunks generated by this story

- [ ] Chunk 9.X — Mission Control page with embedded Langfuse trace
- [ ] Chunk 9.Y — Persist langfuse_trace_id on every scan_run

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Manual test: complete a scan, click "View trace", verify the span tree is complete
- [ ] OpenTelemetry: every orchestrator invocation produces a Langfuse trace within 30 seconds (NFR-011)
- [ ] Step Report produced; user committed

---

## US-009: See published eval metrics

**As** Maya, evaluating whether to trust AuditPilot's AI outputs
**I want** to see the latest TPR, TNR, and Cohen's kappa for the LLM judge alongside the Promptfoo gold-set pass rate
**So that** I can verify the AI quality claims independently rather than taking them on faith

### Acceptance criteria (Gherkin)

```gherkin
GIVEN the eval suite has run on the most recent main commit
WHEN I open /eval-status (public page)
THEN I see TPR, TNR, and Cohen's kappa as numbers (not "X%")
AND I see the Promptfoo gold-set pass rate broken down by category (control mapping, citation faithfulness, policy structure, questionnaire judge)

GIVEN any eval metric falls below the threshold (TPR/TNR < 0.85, kappa < 0.70)
WHEN the page renders
THEN the failing metric is highlighted in red
AND a link to docs/evals/judge-validation.md explains the methodology

GIVEN I want to verify the methodology myself
WHEN I click "Methodology"
THEN a link to docs/evals/judge-validation.md and docs/evals/failure-modes.md opens
AND each gold-set case (in `docs/evals/gold/`) is browsable on GitHub
```

### Why this matters

JTBD-5. Commercial competitors do not publish these numbers. Publishing them — and surfacing them to non-technical users — is the differentiator. Empty placeholders ("X%") are worse than no claim; they signal we have not yet measured.

### Implementation notes (high level only)

- Static data committed to `docs/evals/` after each Sprint 10 eval run.
- Frontend: `apps/web/app/eval-status/page.tsx` reads from `docs/evals/latest.json` (which is generated by CI).
- ADR-0006. SRS NFR-013, NFR-015.

### PLAN.md chunks generated by this story

- [ ] Chunk 10.4 — `scripts/judge_validation.py` produces the metrics
- [ ] Chunk 10.7 — Public `/eval-status` page

### Definition of done

- [ ] All three acceptance criteria pass after Sprint 10 eval run
- [ ] Auto test: CI verifies `docs/evals/latest.json` schema on every commit to `main`
- [ ] Manual test: open the page, click each link, verify the methodology doc is reachable
- [ ] **Constraint:** `docs/evals/gold/` is never edited by automation; only the project owner hand-labels
- [ ] Step Report produced; user committed

---

## US-010: Watch the orchestrator stream tool calls live

**As** Maya
**I want** to see the orchestrator's tool calls as expandable cards streaming in real time during a scan
**So that** I trust the AI is actually using my evidence and not hallucinating answers

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a scan is running
WHEN the orchestrator calls compliance-kb-mcp.search_controls("encryption at rest")
THEN a Tool card appears in the chat surface within 1 second
AND the card shows the tool name, the typed input, and (when complete) the typed output

GIVEN a tool call returns a long result
WHEN the card initially renders
THEN the result is collapsed by default with a "Show details" toggle
AND opening the toggle pretty-prints the JSON

GIVEN a tool call fails (e.g. GitHub MCP returns 502)
WHEN the orchestrator handles the failure
THEN the Tool card shows a red "Failed" state with the error message
AND the orchestrator continues processing subsequent tool calls (one failure does not abort the scan)
```

### Why this matters

JTBD-1 + JTBD-6. The streaming surface is what distinguishes AuditPilot from a black-box compliance tool. If Maya can watch the orchestrator interrogate her evidence, the system feels transparent. If results just appear in the grid, it feels like magic — and magic is what users distrust.

### Implementation notes (high level only)

- Frontend: AI SDK 6 `useChat` with `onToolCall` rendering. Tool card components from AI Elements.
- Backend: AuditOrchestrator emits AI SDK 6 `tool-call` and `tool-result` UIMessage parts. ADR-0003.
- SRS FR-021.

### PLAN.md chunks generated by this story

- [ ] Chunk 4.2 — Render tool-call typed parts as Tool cards from AI Elements

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Vitest snapshot for Tool card states (pending / success / failure)
- [ ] Manual test: run a scan, eyeball at least one of each card state
- [ ] Step Report produced; user committed

---

## US-011: Draft an Incident Response Plan

**As** Maya, who has never written an Incident Response Plan before
**I want** to ask the orchestrator to draft an Incident Response Plan grounded in my actual control posture
**So that** I have a starting document to review and adopt instead of authoring from a blank page

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a recent scan_run with at least one PASSING control in CC7 (System Operations)
WHEN I navigate to /policies and type "Draft an Incident Response Plan"
THEN an SSE stream starts on POST /chat with intent="draft_policy"
AND the orchestrator emits text-delta parts that render in the BlockNote editor live

GIVEN the orchestrator finishes the draft
WHEN the workflow hits HumanReviewGate
THEN a "Pending review" banner appears with Approve / Edit / Reject buttons
AND the draft references at least 3 TSC control IDs as inline footnotes (FR-026)

GIVEN I have no scan_run yet
WHEN I attempt to draft a policy
THEN a non-blocking warning explains the policy will be a generic template (less grounded)
AND I can choose to proceed or run a scan first
```

### Why this matters

JTBD-3. The blank-page problem is real. Maya knows SOC 2 needs an Incident Response Plan but does not know what sections to include or what tone to use. A draft grounded in her actual control posture (her own MFA settings, her own branch protection setup) is 10x more useful than a generic template — and 100x faster than writing from scratch.

### Implementation notes (high level only)

- Frontend: `apps/web/app/policies/page.tsx` with assistant-ui chat (left pane) + BlockNote editor (right pane).
- Backend: orchestrator calls `policy-template-mcp.get_template("incident_response")`, then `compliance-kb-mcp.search_controls` and `evidence-store-mcp.search_evidence` for grounding, then drafts. ADR-0005, ADR-0007.
- SRS FR-023, FR-026.

### PLAN.md chunks generated by this story

- [ ] Chunk 6.3 — PolicyDrafterAgent node emits Markdown policy with citations
- [ ] Chunk 6.4 — `/policies` route with assistant-ui + BlockNote

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies `state.draft_policy` contains at least 3 control ID citations
- [ ] Manual test: draft a real Incident Response Plan, verify the citations match real CC7 controls
- [ ] Step Report produced; user committed

---

## US-012: Edit a draft policy in the workspace

**As** Maya
**I want** to edit the draft policy directly in the BlockNote editor before approving it
**So that** I can correct company-specific details without going through reject-and-redraft

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a policy draft is in pending_review state in the editor
WHEN I edit a paragraph (e.g. change "Acme Corp" to "MyCo")
THEN the edits persist locally and are visible if I refresh
AND the edit history is preserved in the policy_revisions table

GIVEN I have edited the draft
WHEN I click "Save edits and download"
THEN POST /chat/resume is called with decision="edit" and the new content
AND the orchestrator finalizes the document with my edits and uploads to R2
AND the download starts immediately

GIVEN I have edited the draft and clicked Save
WHEN I look at the policy detail page later
THEN the final approved version reflects my edits, not the original AI draft
```

### Why this matters

JTBD-3. AI drafts are starting points, not finished documents. Maya needs to inject company-specific context (real names, real policies, internal terminology) before she would publish anything. The workspace-style UX (chat + editor side by side) is what differentiates AuditPilot from a one-shot generation tool.

### Implementation notes (high level only)

- Frontend: BlockNote editor with autosave to local state; on Save, POST to `/chat/resume`.
- Backend: `Command(resume=HumanReviewPayload(decision="edit", edited_content=...))`. ADR-0007.
- Data: `policy_revisions` table tracks edits with editor field ("user" or "agent").

### PLAN.md chunks generated by this story

- [ ] Chunk 6.4.1 — BlockNote editor wiring with local autosave
- [ ] Chunk 6.4.2 — `policy_revisions` table and write path

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies the orchestrator uses `edited_content` (not the original draft) when finalizing
- [ ] Manual test: edit a paragraph, save, refresh, verify the edit persists
- [ ] Step Report produced; user committed

---

## US-013: Approve a policy via HITL gate

**As** Maya
**I want** to explicitly approve a draft policy with one click after reviewing it
**So that** the workflow only proceeds when I have actually consented (not just because time elapsed)

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a policy draft is in pending_review state
WHEN I click "Approve and download"
THEN POST /chat/resume is called with decision="approve"
AND the orchestrator's LangGraph graph resumes from the interrupt checkpoint (FR-049)
AND the final document is uploaded to R2

GIVEN I close my browser before approving
WHEN I return hours or days later and reopen /policies
THEN the draft is still in pending_review
AND I can approve, edit, or reject from where I left off (PostgresSaver durability)

GIVEN the orchestrator approves but the R2 upload fails
WHEN the failure surfaces
THEN the action returns to pending_review with a clear "upload failed, retry" message
AND no half-approved state exists in the database

GIVEN my JWT expires while I have a draft policy open in the workspace for review
WHEN I click "Approve and download"
THEN the frontend transparently refreshes the JWT via the Supabase Auth refresh token
AND the resume call proceeds with the new JWT
AND I see no interruption in the workflow

GIVEN I have two browser tabs open on the same draft policy
WHEN I click "Approve" in one tab
THEN the second tab updates within 5 seconds to show the approved state via SWR revalidation
AND clicking "Approve" again in the second tab returns 409 Conflict with a "already approved" message
```

### Why this matters

JTBD-3. The HITL gate is the architectural mechanism that distinguishes "the AI suggested this" from "the human decided this" (ADR-0007). Approval must be explicit, the gate must survive process restarts, and the resume must be idempotent. These are not nice-to-have properties — they are the defining contract of the read-only-by-design model (ADR-0004).

### Implementation notes (high level only)

- Frontend: Approve button → POST `/chat/resume` → SSE stream resumes.
- Backend: LangGraph `Command(resume=HumanReviewPayload(decision="approve"))`. PostgresSaver checkpointer.
- ADR-0007. SRS FR-046 through FR-049.

### PLAN.md chunks generated by this story

- [ ] Chunk 6.1 — HumanReviewGate node using LangGraph `interrupt()`
- [ ] Chunk 6.2 — Pending Actions queue UI on `/dashboard` showing draft cards

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest test that interrupts a graph, restarts the FastAPI process, then resumes; verifies state is preserved
- [ ] Manual test: start a draft, close browser, reopen 1 hour later, approve; verify the document is generated correctly
- [ ] Langfuse trace shows `HumanReviewGate` span with status `INTERRUPTED` then closed with `decision: approve`
- [ ] Step Report produced; user committed

---

## US-014: Reject a policy with a reason and re-draft

**As** Maya, reviewing a draft I do not want to ship
**I want** to reject the draft with a one-paragraph reason and have the orchestrator re-draft incorporating that reason
**So that** the system learns from my feedback within the same session rather than forcing me to start over

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a draft policy is in pending_review
WHEN I click "Reject" and enter "This sounds too generic; reference our specific incident severity tiers"
THEN POST /chat/resume is called with decision="reject" and rejection_reason
AND the orchestrator re-runs PolicyDrafterAgent with the rejection_reason injected into the prompt
AND a new draft appears that addresses the feedback

GIVEN I reject the same draft 3 times in a row
WHEN the orchestrator detects the pattern
THEN a "We're not converging — let's collaborate" message offers to start a chat thread instead
AND the draft is marked as needing manual authoring

GIVEN I reject and the orchestrator's re-draft also gets rejected
WHEN I look at the policy_revisions table
THEN every rejection_reason is preserved in chronological order with the corresponding draft snapshot
```

### Why this matters

JTBD-3. The first AI draft is rarely the final version. Without a re-draft loop, Maya hits a wall on the first reject and has to author from a blank page anyway. With the loop, she can guide the system to a usable result in three to five iterations. The 3-strike circuit-breaker prevents an infinite loop on requests the LLM cannot satisfy.

### Implementation notes (high level only)

- Frontend: Reject modal with textarea for reason.
- Backend: `state.rejection_reasons[-1]` injected into the next PolicyDrafterAgent prompt as context. ADR-0007. SRS FR-050.
- Circuit breaker: count `rejection_reasons` per `draft_policy_id`; at 3, switch to manual-authoring mode.

### PLAN.md chunks generated by this story

- [ ] Chunk 6.4.3 — Reject modal + reason capture
- [ ] Chunk 6.4.4 — Re-draft loop with rejection_reason in context
- [ ] Chunk 6.4.5 — 3-strike circuit breaker

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies that rejection_reason is in the orchestrator's prompt context on the next invocation
- [ ] Manual test: reject 3 times, verify the circuit breaker fires
- [ ] Step Report produced; user committed

---

## US-015: Download a policy as DOCX

**As** Maya
**I want** to download an approved policy as a Word document with proper formatting and citations
**So that** I can publish it to my company wiki or send it to my CEO without re-formatting

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a policy is in approved state
WHEN I click "Download .docx"
THEN GET /api/policies/{id}/download?format=docx returns a 302 to a pre-signed R2 URL
AND the URL has a 15-minute TTL
AND the downloaded file opens in Microsoft Word with proper headings, footnote-style citations, and AuditPilot watermark

GIVEN I want a Markdown copy instead
WHEN I click "Download .md"
THEN GET /api/policies/{id}/download?format=md returns the same content as Markdown

GIVEN the policy was edited via US-012 before approval
WHEN I download
THEN the downloaded file reflects my edits (not the original AI draft)
```

### Why this matters

JTBD-3. The output of policy drafting must be in a format Maya can use immediately. Markdown is fine for engineers; DOCX is what executives and lawyers expect. Both are required for a real-world adoption flow.

### Implementation notes (high level only)

- Backend: `GET /api/policies/{id}/download?format=md|docx` → 302 to pre-signed R2 URL. ADR-0008.
- DOCX generation: `python-docx` library, run inline as part of policy finalization.
- SRS FR-027.

### PLAN.md chunks generated by this story

- [ ] Chunk 6.5 — "Approve and download .docx" button + endpoint

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: integration test downloads a DOCX and verifies headings + citations are preserved
- [ ] Manual test: open the DOCX in Word and Google Docs, verify formatting renders correctly
- [ ] Step Report produced; user committed

---

## US-016: Upload a SIG-Lite XLSX for auto-fill

**As** Maya, who has just received a SIG-Lite questionnaire from a prospective customer
**I want** to drag-drop the XLSX into the questionnaire workspace and watch the orchestrator fill answers
**So that** I can submit the response within a day rather than spending 16 hours copy-pasting

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a valid SIG-Lite XLSX of ≤ 10 MB
WHEN I drag it onto /questionnaire
THEN POST /api/questionnaire/upload accepts the file (returns 202 with run_id)
AND the parser extracts exactly 128 questions from the standard SIG-Lite v2026 fixture (FR-030)
AND the orchestrator clusters them into 10–20 domains (FR-031)

GIVEN parsing has completed
WHEN the orchestrator drafts answers
THEN each cell shows status (pending / drafted / flagged) live in the workspace
AND the progress indicator shows X / 128 cells filled

GIVEN I upload an unsupported format (e.g. SIG Core when only SIG-Lite is supported in v1)
WHEN parsing fails
THEN POST /api/questionnaire/upload returns 422 with a clear error
AND the run is marked failed with reason "format not supported in v1"
```

### Why this matters

JTBD-2. This is the demo's killer feature. Every founding engineer who has done a Fortune 500 deal has felt this pain. The 16-hours-to-2-hours compression is the moment that converts skeptics into believers in the demo video.

### Implementation notes (high level only)

- Frontend: react-dropzone + progress overlay.
- Backend: `POST /api/questionnaire/upload` (multipart). `questionnaire-mcp.parse_xlsx`. ADR-0005.
- SRS FR-029, FR-030, FR-031.

### PLAN.md chunks generated by this story

- [ ] Chunk 7.2 — SIG-Lite XLSX parser tool
- [ ] Chunk 7.3 — `cluster_questions` tool
- [ ] Chunk 7.5 — Questionnaire workspace UI

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest fixture (a real anonymized SIG-Lite) parses to exactly 128 questions
- [ ] Manual test: upload the fixture, watch live cells fill
- [ ] Step Report produced; user committed

---

## US-017: Review flagged questionnaire cells

**As** Maya
**I want** the workspace to highlight cells the orchestrator was not confident about and let me review them in a focused queue
**So that** I can spend my review time only on the cells that need it

### Acceptance criteria (Gherkin)

```gherkin
GIVEN the orchestrator has finished drafting answers and flagged 58 cells (confidence < 0.70)
WHEN I open the questionnaire workspace
THEN the cells are visually highlighted in yellow
AND a "Review flagged (58)" filter button shows only flagged cells

GIVEN I am reviewing a flagged cell
WHEN I edit the answer or accept the draft as-is
THEN the cell's flagged status clears and the cell turns green
AND the change persists if I refresh

GIVEN I want to add a citation to a flagged cell
WHEN I open the citation picker
THEN I can attach an evidence ID from the evidence-store
AND the citation appears as a comment in the downloaded XLSX
```

### Why this matters

JTBD-2. The auto-fill is only valuable if Maya trusts it. The yellow-flagged review surface is the trust boundary: green cells she trusts at-a-glance, yellow cells she reviews carefully. Without this, she would be forced to re-read all 128 cells, defeating the time savings.

### Implementation notes (high level only)

- Frontend: questionnaire workspace grid component with filter.
- Backend: `GET /api/questionnaire/{run_id}/answers?flagged=true|false`. ADR-0005.
- SRS FR-034.

### PLAN.md chunks generated by this story

- [ ] Chunk 7.5.1 — Flagged-only filter + cell editing
- [ ] Chunk 7.5.2 — Citation picker

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies cells with confidence < 0.70 have flagged=true
- [ ] Manual test: review a flagged cell, edit, save, verify
- [ ] Step Report produced; user committed

---

## US-018: Download the filled questionnaire XLSX

**As** Maya
**I want** to download the filled questionnaire as XLSX with the original formatting plus my answers and citations as comments
**So that** I can hand it to the prospective customer in the format they expect

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a questionnaire_run is in ready state
WHEN I click "Download filled XLSX"
THEN GET /api/questionnaire/{run_id}/download returns 302 to a pre-signed R2 URL
AND the downloaded XLSX has the original SIG-Lite structure preserved
AND each filled cell has a comment containing the citation evidence ID

GIVEN cells are flagged for review
WHEN I download
THEN flagged cells are highlighted yellow in the XLSX
AND a "flagged" column lets the customer reviewer know which cells were less confident

GIVEN I click download but R2 is unreachable
WHEN the failure surfaces
THEN a clear error message offers a retry button
AND no half-downloaded file is presented
```

### Why this matters

JTBD-2. The output must match the format the customer expects. Security teams at Fortune 500 customers do not accept Markdown. They want SIG-Lite XLSX with cell comments. AuditPilot meets the customer in their format.

### Implementation notes (high level only)

- Backend: `GET /api/questionnaire/{run_id}/download` → 302 to R2 pre-signed URL. ADR-0008.
- XLSX generation: `openpyxl` to inject answers and comments into the original template.
- SRS FR-035.

### PLAN.md chunks generated by this story

- [ ] Chunk 7.6 — "Download filled XLSX" + flagged-for-review queue

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: integration test downloads the XLSX and verifies cell comments contain citations
- [ ] Manual test: open in Excel, verify formatting matches original
- [ ] Step Report produced; user committed

---

## US-019: Run an adversarial mock readiness challenge

**As** Maya, preparing for a real external readiness review in 8 weeks
**I want** to trigger an adversarial mock readiness challenge that grills my evidence and surfaces gaps I might miss
**So that** I find weak spots before a real reviewer does

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a recent scan_run with at least 10 PASSING controls
WHEN I click "Run adversarial mock readiness challenge"
THEN POST /api/mock-audit/run is called with my scan_run_id
AND the SSE stream shows the orchestrator dispatching to AdversarialAuditor over A2A v1.0
AND adversarial findings stream into the Findings panel as they are produced

GIVEN the AdversarialAuditor exhausts its $0.50 budget mid-run
WHEN the budget callback raises BudgetExceededError
THEN the run is marked status=budget_exceeded
AND any findings produced before the breach are still saved
AND a clear message explains the budget limit

GIVEN I trigger the challenge while another mock run for the same scan_run_id is in progress
WHEN the request arrives
THEN it returns 409 Conflict with a "previous run still in progress" message
AND no second AdversarialAuditor task is started

GIVEN the AdversarialAuditor's Cloud Run service is cold-starting and takes >10 seconds to respond to the first A2A call
WHEN the orchestrator polls /a2a/tasks/{task_id}
THEN it tolerates the cold-start delay up to 60 seconds via the polling pattern documented in system-design §3.5
AND no findings are dropped if they arrive after the polling timeout (the result remains accessible via GET /api/mock-audit/{run_id})
```

### Why this matters

JTBD-4. The adversarial challenge is the show-stopper agent for demos. Maya watches an adversarial reviewer agent grill her evidence in real time. It surfaces objections she would have missed, all 8 weeks before the real external reviewer arrives. ADR-0002 keeps this as a separate Cloud Run service for context isolation.

### Implementation notes (high level only)

- Frontend: "Run adversarial mock readiness challenge" button, Findings panel with live-streaming objections.
- Backend: orchestrator → A2A v1.0 → `apps/auditor`. ADR-0002, ADR-0003. SRS FR-041 through FR-045.
- Budget enforcement: LiteLLM callback raising `BudgetExceededError` at $0.50 cumulative cost.

### PLAN.md chunks generated by this story

- [ ] Chunk 8.2 — AdversarialAuditor Pydantic AI agent with budget cap
- [ ] Chunk 8.3 — A2A v1.0 server with signed AgentCard
- [ ] Chunk 8.4 — Orchestrator calls auditor via RemoteA2aAgent
- [ ] Chunk 8.5 — "Run mock readiness challenge" button on dashboard

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: integration test mocks the A2A endpoint and verifies findings flow back into the gap report
- [ ] Auto test: inject a $0.001 cap and verify the run terminates with status=budget_exceeded
- [ ] Manual test: run a real challenge against a fixture scan, eyeball findings
- [ ] Langfuse traces visible for both orchestrator and auditor processes
- [ ] Step Report produced; user committed

---

## US-020: Download the gap report from a mock readiness challenge

**As** Maya
**I want** to download a Markdown gap report combining my readiness scan findings with the AdversarialAuditor's objections
**So that** I have one document I can share with my CEO that shows what to fix before the real reviewer arrives

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a mock_audit_run completed with at least one finding
WHEN I click "Download gap report"
THEN GET /api/mock-audit/{run_id}/report returns 302 to a pre-signed R2 URL
AND the Markdown report contains: scan summary, list of FAILING controls, list of adversarial objections (severity-sorted), recommended next steps

GIVEN the report has been generated
WHEN I open the file
THEN every objection cites the relevant control_id
AND every recommended next step links to either a Pending Action or a draft policy

GIVEN no findings were produced (e.g. all controls passing, AdversarialAuditor returned empty)
WHEN I click "Download gap report"
THEN the report still generates with a clear "No findings — adversarial reviewer found no objections" section
AND a recommendation to keep monitoring drift is included
```

### Why this matters

JTBD-4. The download is the closure. Maya runs the challenge, watches the findings, then needs a single artifact to circulate. A scattered list of cards is not enough — a real document is. The gap report is also the evidence that the adversarial step happened, which is the artifact a reviewer or hiring manager would inspect.

### Implementation notes (high level only)

- Backend: `GET /api/mock-audit/{run_id}/report` → 302 to pre-signed R2 URL. ADR-0008.
- Report rendering: Markdown templating in the orchestrator after merging adversarial findings into state.
- SRS FR-044.

### PLAN.md chunks generated by this story

- [ ] Chunk 8.6 — Findings merge into gap report

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: integration test verifies report contents against a known fixture
- [ ] Manual test: run a challenge, download the report, verify it is readable and well-structured
- [ ] Step Report produced; user committed

---

## US-021: Run mock readiness challenge inside a self-hosted deployment

**As** the security lead at a 60-person SaaS company evaluating AuditPilot for self-hosted use
**I want** to run the adversarial mock readiness challenge entirely inside our VPC
**So that** our actual readiness evidence never leaves our network

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have deployed AuditPilot via `docker compose up` on infrastructure I control
AND I have configured environment variables for our internal Postgres + Redis + R2-compatible storage
WHEN I trigger a mock readiness challenge from the dashboard
THEN the AdversarialAuditor runs in a container on my own infrastructure
AND no traffic leaves my VPC except outbound LLM API calls to my chosen provider

GIVEN I want to use a self-hosted LLM (e.g. vLLM) instead of Gemini or Anthropic
WHEN I set LITELLM_PROVIDER=vllm and LITELLM_BASE_URL=http://my-vllm:8000
THEN the orchestrator and AdversarialAuditor route LLM calls through my self-hosted endpoint
AND no telemetry from these calls is sent to LangChain, Anthropic, or Google

GIVEN I want full traceability with no third-party data sharing
WHEN I configure LANGFUSE_HOST=http://my-langfuse:3000 (self-hosted Langfuse)
THEN every Langfuse trace lands on my own Postgres-backed Langfuse instance
AND no data is sent to Langfuse Cloud
```

### Why this matters

JTBD-4 from the security-lead perspective. Self-hosting is the differentiator that lets a company use AuditPilot for real readiness work without sending evidence outside their VPC. The Apache 2.0 license + Docker Compose + LiteLLM provider abstraction make this possible; this story makes it explicit.

### Implementation notes (high level only)

- Existing Docker Compose covers the Postgres + Redis + FastAPI + Auditor + Web stack.
- LiteLLM already abstracts the provider; setting an env var swaps it.
- Langfuse self-hosting is a documented path in `docs/runbooks/self-host.md` (TODO in Sprint 11 polish).

### PLAN.md chunks generated by this story

- [ ] Chunk 11.X — `docs/runbooks/self-host.md` runbook for self-hosted deployment
- [ ] Chunk 11.Y — Verify LiteLLM provider env var flips at runtime in integration test

### Definition of done

- [ ] All three acceptance criteria pass in a fresh VPC test (manually verified once)
- [ ] Auto test: integration test with `LITELLM_PROVIDER=mock` verifies no outbound calls to Anthropic/Gemini
- [ ] Manual test: deploy to a fresh GCP project with no Langfuse Cloud key, verify the system runs with self-hosted Langfuse fallback
- [ ] Step Report produced; user committed

---

## US-022: Self-host AuditPilot inside our VPC

**As** the security lead at a scaling startup
**I want** a one-command deploy of AuditPilot inside my own infrastructure
**So that** I can adopt the project without trusting our compliance evidence to a third party

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a clean machine with Docker and Docker Compose installed
WHEN I clone the AuditPilot repo and run `cp .env.example .env && docker compose up`
THEN the full stack starts in under 5 minutes
AND a working dashboard is reachable at http://localhost:3000
AND every required env var is documented in `.env.example` with a comment explaining its purpose

GIVEN I want to deploy to my own Kubernetes cluster
WHEN I read `docs/runbooks/deploy-self-hosted.md`
THEN the doc explains the option of (a) `docker compose -f docker-compose.prod.yml up` on a single VM, OR (b) Helm chart at `infra/helm/` (deferred to v2 per PRD NG-8)
AND I can choose path (a) for v1 and run AuditPilot in production within a day

GIVEN I have my own Postgres + Redis instances
WHEN I set DATABASE_URL and REDIS_URL to point at them
THEN the app skips the bundled Postgres and Redis containers
AND the migrations run cleanly against my external Postgres on `docker compose run api alembic upgrade head`
```

### Why this matters

JTBD-6 from the security-lead perspective. The Apache 2.0 license + comprehensive self-host story is the key reason a security-conscious team would choose AuditPilot over the closed-source commercial competition. PRD §2.2.

### Implementation notes (high level only)

- Docker Compose multi-stage Dockerfiles per `docker-patterns` skill.
- `docs/runbooks/self-host.md` is the canonical doc.
- `.env.example` lists every variable with default + purpose.

### PLAN.md chunks generated by this story

- [ ] Chunk 0F.2 — Empty docker-compose.yml with Postgres + Redis (already in PLAN.md)
- [ ] Chunk 11.Z — `.env.example` with full documentation
- [ ] Chunk 11.AA — `docs/runbooks/self-host.md`

### Definition of done

- [ ] All three acceptance criteria pass on a fresh Linux VM
- [ ] Auto test: GitHub Actions job that boots the full stack via Docker Compose and hits `/health`
- [ ] Manual test: a contributor clones the repo, follows the self-host doc, reports back time-to-running
- [ ] Step Report produced; user committed

---

## US-023: Fork an MCP server for a new domain

**As** an AI engineer learning AuditPilot as a reference architecture
**I want** to fork `compliance-kb-mcp` and adapt it for a different compliance framework (e.g. ISO 27001)
**So that** I can use the same agent topology for my own domain

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I want to build an ISO 27001 readiness tool
WHEN I read `packages/compliance-kb-mcp/README.md` and the published Pydantic schemas
THEN the README explains the data model, the FastMCP entrypoint, and the npm + PyPI publish flow
AND I can `git clone` the repo, copy `packages/compliance-kb-mcp` to a new directory, swap the SOC 2 dataset for ISO 27001 controls, and have a working MCP server in under a day

GIVEN I have forked and run `pnpm install && uv sync`
WHEN I run the test suite from inside `packages/compliance-kb-mcp/`
THEN tests pass without any project-specific assumptions (no hardcoded paths, no shared utilities outside the package)
AND `npm pack --dry-run` and `uv build` produce clean artifacts

GIVEN I have my fork running locally
WHEN I configure my own LangGraph agent to connect via stdio
THEN the agent can call `lookup_control`, `search_controls`, `list_controls` against my ISO 27001 dataset
AND the schemas validate via Pydantic v2 `extra="forbid"`
```

### Why this matters

JTBD-6. The MCP server count is a portfolio signal, but the real value to the OSS community is forkability. If a fellow engineer can adopt AuditPilot's MCP pattern in under a day, the project succeeds as a reference architecture. PRD §2.3.

### Implementation notes (high level only)

- ADR-0005 specifies the publish + structure model.
- Each MCP package has its own README with quick-start.
- The `mcp-scaffold` skill (`/mcp-scaffold <name>`) generates the boilerplate.

### PLAN.md chunks generated by this story

- [ ] Chunk 1.7 — README + LICENSE + CHANGELOG complete (already in PLAN.md)
- [ ] Chunk 11.AB — Add a "How to fork" section to each MCP server README

### Definition of done

- [ ] All three acceptance criteria pass when a contributor follows the README
- [ ] Auto test: every MCP package has a `tests/` directory passing with no project-shared utilities imported
- [ ] Manual test: maintainer forks `compliance-kb-mcp` to `iso-27001-kb-mcp` once and times it
- [ ] Step Report produced; user committed

---

## US-024: Wire Promptfoo and judge validation into our CI

**As** an AI engineer on a different LLM-powered project
**I want** to copy AuditPilot's eval pipeline (Promptfoo + judge validation script) into my own CI
**So that** I get a regression-blocking eval gate without having to design one from scratch

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I read `docs/evals/judge-validation.md` and `.github/workflows/eval.yml`
WHEN I want to apply the same pattern to my own LLM project
THEN the docs explain the gold-set structure, the YAML format, the judge-validation methodology (TPR/TNR/Cohen's kappa), and the CI gate
AND the threshold values (TPR ≥ 0.85, TNR ≥ 0.85, kappa ≥ 0.70) are explained with a "why these numbers" rationale

GIVEN I have my own gold set in YAML
WHEN I copy `scripts/judge_validation.py` to my repo
THEN the script runs without AuditPilot-specific imports
AND it reads my gold set, runs my judge prompt, and outputs `judge-validation.json` + a Markdown summary

GIVEN my eval suite produces a regression
WHEN the CI workflow runs on a PR
THEN the eval gate posts a comment with the failing cases, the regression delta vs. baseline, and a Langfuse trace URL for each
AND the merge is blocked
```

### Why this matters

JTBD-5 + JTBD-6. Most teams using LLM-as-judge never validate their judge. AuditPilot's eval discipline is one of the headline differentiators (ADR-0006). Making it portable to other projects multiplies the value of the methodology.

### Implementation notes (high level only)

- ADR-0006 specifies the methodology.
- `scripts/judge_validation.py` is project-agnostic by design.
- `.github/workflows/eval.yml` ships in Sprint 10.

### PLAN.md chunks generated by this story

- [ ] Chunk 10.4 — `scripts/judge_validation.py` produces the metrics (already in PLAN.md)
- [ ] Chunk 10.7 — Public `/eval-status` page (already in PLAN.md)
- [ ] Chunk 11.AC — Add "How to apply this in your own project" section to `docs/evals/judge-validation.md`

### Definition of done

- [ ] All three acceptance criteria pass on a fresh repo using the script
- [ ] Auto test: a "smoke" repo exercises the script against a tiny synthetic gold set and asserts the JSON output schema
- [ ] Manual test: maintainer applies the script to a different LLM project (Mentivo) once and times it
- [ ] Step Report produced; user committed

---

## US-025: Receive a Pending Action when a control drifts

**As** Maya
**I want** the drift watcher to surface a Pending Action card when a previously-PASSING control regresses
**So that** I notice the regression without manually re-running scans

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a recent scan_run where CC8.1 (branch protection) was PASSING
AND the drift watcher cron runs at 12:00 UTC and finds branch protection has been removed from main
WHEN I open /dashboard at 12:05 UTC
THEN a new Pending Action card shows "CC8.1 drifted: branch protection on main was removed"
AND the card has `event_type=config_changed`, severity=high, and a deep link to the GitHub branch protection settings page
AND the suggested_fix text drafts the exact GitHub setting to re-enable

GIVEN a control's evidence flapped between PASSING and NOT_ASSESSED in two consecutive cron runs
WHEN the drift watcher evaluates it
THEN no drift event is emitted (the 2-scan flap protection in system-design §13.3 absorbs noise)

GIVEN I have a control that was added since my last scan
WHEN the drift watcher runs against the new evidence
THEN no drift event fires for the new control (only baseline records are written)
```

### Why this matters

JTBD-7. Drift is the difference between point-in-time readiness and continuous readiness. Without drift detection, a single deploy that breaks a control silently regresses the company's posture. The Pending Action surface keeps Maya in the loop without forcing her to re-run scans manually.

### Implementation notes (high level only)

- `drift-watcher-mcp` does the diff (system-design §13.2).
- Vercel Cron triggers every 6 hours (ADR-0008 deployment).
- Pending Actions are typed via `DriftEventCard` (system-design §13.4).

### PLAN.md chunks generated by this story

- [ ] Chunk 9.1 — Drift detector function (already in PLAN.md)
- [ ] Chunk 9.2 — Vercel Cron schedule (already in PLAN.md)
- [ ] Chunk 9.X — DriftEventCard rendering on dashboard

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest fixture diffs two snapshots and asserts the drift events match
- [ ] Manual test: disable branch protection on a test repo, wait for the next cron, verify the action card appears
- [ ] OpenTelemetry span on `drift.scan` job
- [ ] Step Report produced; user committed

---

## US-026: Dismiss a false-positive drift event

**As** Maya
**I want** to dismiss a drift event that I judge to be a false positive
**So that** the dashboard does not nag me about an event that does not actually represent a regression

### Acceptance criteria (Gherkin)

```gherkin
GIVEN a drift event card is on my Pending Actions queue
WHEN I click "Dismiss" with reason "intentional removal of stale rule"
THEN the card moves to "Dismissed" state immediately
AND the reason is persisted to drift_events.dismissed_reason
AND the card does not re-appear on the next cron run for the same control unless a new diff fires

GIVEN I dismissed a drift event a week ago for CC6.1
AND the same configuration flips again to a different state today
WHEN the next cron runs
THEN a new drift event fires (different content_hash, so it is not the same event)
AND I see a fresh card

GIVEN I want to see all dismissed events
WHEN I open /drift-events?status=dismissed
THEN a paginated list of dismissed events with reasons is shown
AND I can un-dismiss any event by clicking "Restore" (returns to open)
```

### Why this matters

JTBD-7. Drift detection without a dismiss path becomes noise quickly. False positives are inevitable in heuristic-based detection; the user needs an escape valve that does not require code changes.

### Implementation notes (high level only)

- `drift_events` table has `status` and `dismissed_reason` columns (system-design §13.4).
- `PATCH /api/drift/events/{event_id}` accepts the dismiss payload.
- Re-firing logic is content-hash-based; a fresh hash means a fresh event.

### PLAN.md chunks generated by this story

- [ ] Chunk 9.X — Dismiss button on drift event cards
- [ ] Chunk 9.Y — `PATCH /api/drift/events/{id}` endpoint

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies dismissed events do not re-fire on the same content_hash
- [ ] Manual test: dismiss a real event, verify it is gone; create new diff, verify new event appears
- [ ] Step Report produced; user committed

---

## US-027: Draft an Access Control Policy

**As** Maya
**I want** the orchestrator to draft an Access Control Policy grounded in my actual control posture
**So that** I have a starting document covering CC6 (Logical Access) for review and adoption

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a recent scan_run with at least one PASSING control in CC6
WHEN I navigate to /policies and click "Draft Access Control Policy"
THEN the orchestrator drafts a Markdown policy referencing CC6.1, CC6.2, CC6.3 (or whichever apply)
AND the draft includes sections: Purpose, Scope, Roles, Access Provisioning, Periodic Review, Revocation, Exceptions

GIVEN the orchestrator finishes the draft
WHEN the workflow hits HumanReviewGate
THEN I can Approve / Edit / Reject identically to US-013

GIVEN I have multiple drafts of an Access Control Policy
WHEN I open /policies
THEN they are listed with version numbers and approval timestamps
AND I can see which version is the "current" approved one
```

### Why this matters

JTBD-3. Access Control is one of the four core SOC 2 policies. PRD §6.1 lists it as Must-tier. This story expands US-011 (Incident Response Plan) to the second of four policies.

### Implementation notes (high level only)

- Re-uses the orchestrator + `policy-template-mcp` flow from US-011.
- New template: `packages/policy-template-mcp/templates/access_control.md`.

### PLAN.md chunks generated by this story

- [ ] Chunk 6.X — `access_control.md` template in policy-template-mcp
- [ ] Chunk 6.Y — Eval coverage for Access Control draft (smoke-tested, full coverage in v1.5 per OAQ from rationale doc)

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies citations include at least 3 CC6 controls
- [ ] Manual test: draft a real Access Control Policy, eyeball quality
- [ ] Step Report produced; user committed

---

## US-028: Draft a Change Management Policy

**As** Maya
**I want** the orchestrator to draft a Change Management Policy grounded in my actual control posture
**So that** I have a starting document covering CC8 (Change Management) for review

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a recent scan_run with PASSING controls in CC8
WHEN I navigate to /policies and click "Draft Change Management Policy"
THEN the orchestrator drafts a Markdown policy referencing CC8.1 (PR review), CC8.2 (testing), CC8.3 (rollback)
AND the draft includes sections: Purpose, Scope, Change Categories, Approval Workflow, Testing Requirements, Rollback Procedures, Emergency Changes

GIVEN the orchestrator finds my CC8 controls have evidence of branch protection but no evidence of staging deploys
WHEN it drafts the policy
THEN the draft notes the gap and suggests "Add a staging environment deploy step before production deploys"
AND the gap surfaces as a separate Pending Action

GIVEN I edit the draft to mention our specific Slack channel for change approval
WHEN I save and approve
THEN the final document includes my edit
```

### Why this matters

JTBD-3. CC8 is one of the four SOC 2 categories that real CPAs heavily inspect during a Type II review. A drafted Change Management Policy is the document Maya needs but has historically had to write from scratch.

### Implementation notes (high level only)

- Re-uses orchestrator + policy-template-mcp from US-011.
- Template: `packages/policy-template-mcp/templates/change_management.md`.

### PLAN.md chunks generated by this story

- [ ] Chunk 6.X — `change_management.md` template
- [ ] Chunk 6.Y — Gap-detection logic in the drafter (notes when evidence is incomplete)

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies citations include CC8 controls
- [ ] Manual test: draft + edit + approve flow end to end
- [ ] Step Report produced; user committed

---

## US-029: Draft a Vendor Management Policy

**As** Maya
**I want** the orchestrator to draft a Vendor Management Policy grounded in my actual vendor list (with v1.5 connectors)
**So that** I have a starting document covering CC9 (Vendor Management)

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a recent scan_run with PASSING controls in CC9
WHEN I navigate to /policies and click "Draft Vendor Management Policy"
THEN the orchestrator drafts a Markdown policy referencing CC9.x controls
AND the draft includes sections: Purpose, Scope, Vendor Categories (Critical / Standard / Low-Risk), Onboarding, DPA Tracking, Annual Review, Termination

GIVEN the v1 release does not have Gmail or Calendar connectors
WHEN the orchestrator drafts the policy
THEN the draft uses generic placeholders for "your vendor list" rather than referencing specific vendors
AND a note in the draft explains: "Connect Gmail in v1.5 to auto-populate the vendor list from DPAs in your inbox"

GIVEN I have Gmail connected (post v1.5)
WHEN I draft the same policy
THEN specific vendors discovered via DPA email scanning are referenced in the policy
```

### Why this matters

JTBD-3. CC9 is the fourth Must-tier policy. Even without the Gmail connector in v1, having a placeholder-friendly draft is better than no draft.

### Implementation notes (high level only)

- Same pattern as US-027 / US-028.
- Template: `packages/policy-template-mcp/templates/vendor_management.md`.
- The Gmail-specific path is a v1.5 enhancement.

### PLAN.md chunks generated by this story

- [ ] Chunk 6.X — `vendor_management.md` template
- [ ] Chunk 6.Y — Conditional rendering when Gmail connector is connected

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies the v1 path uses placeholder vendor names
- [ ] Manual test: draft both with and without Gmail connector
- [ ] Step Report produced; user committed

---

## US-030: Re-run a scan with different params

**As** Maya
**I want** to re-run a previous scan with adjusted parameters (e.g. include/exclude specific repositories)
**So that** I can see how scope changes affect my control posture without losing the original scan

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a previous scan_run R1
WHEN I click "Re-run" on R1's detail page and adjust the repo include-list
THEN POST /api/scan-runs is called with `{ source: "rerun", parent_scan_run_id: R1, params_override: {...} }`
AND a new scan_run R2 is created with `parent_scan_run_id` pointing back to R1
AND R1 is unchanged

GIVEN R2 finishes running
WHEN I view the dashboard
THEN R2 is the new "current" scan
AND R1 is still accessible from the scan history list
AND I can switch between viewing either

GIVEN R2 has different control statuses than R1
WHEN I click "Compare to parent"
THEN I land on the diff view (US-031)
```

### Why this matters

Real users iterate. They run a scan, notice an unexpected gap, adjust scope, re-run. Without a re-run flow, they have to start from scratch every time.

### Implementation notes (high level only)

- New column `parent_scan_run_id` on `scan_runs` table.
- `POST /api/scan-runs` accepts the rerun payload.
- System-design §15.1.

### PLAN.md chunks generated by this story

- [ ] Chunk 9.X — Add `parent_scan_run_id` column via Drizzle migration
- [ ] Chunk 9.Y — `Re-run` button on scan_run detail page

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies the parent link is set correctly
- [ ] Manual test: re-run a real scan with a different repo include-list
- [ ] Step Report produced; user committed

---

## US-031: Compare two scan runs side by side

**As** Maya
**I want** to compare two scan runs and see exactly which controls changed status
**So that** I can show my CEO the week-over-week improvement in our SOC 2 posture

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have two scan_runs R1 (from last week) and R2 (from today)
WHEN I open /scan-runs/diff?a=R1&b=R2
THEN GET /api/scan-runs/diff returns a typed ScanRunDiff
AND the frontend renders a side-by-side grid where each control cell shows R1 status vs R2 status
AND regressions (PASSING → FAILING) are red, improvements (FAILING → PASSING) are green

GIVEN R1 had 38 PASSING controls and R2 has 44 PASSING controls
WHEN I view the diff summary
THEN a top-of-page chip shows "+6 controls now PASSING (15.8% improvement)"

GIVEN R2 was a re-run of R1 with different params
WHEN I view the diff
THEN a banner explains: "R2 is a re-run of R1 with adjusted parameters"
AND the banner links to the params_override JSON
```

### Why this matters

Real users want to show progress. Diff views are the proof of progress. Without a diff, the user has to manually compare two scans by opening them in two tabs.

### Implementation notes (high level only)

- New endpoint `GET /api/scan-runs/diff?a=&b=`.
- ScanRunDiff Pydantic model (system-design §15.2).

### PLAN.md chunks generated by this story

- [ ] Chunk 9.X — `GET /api/scan-runs/diff` endpoint
- [ ] Chunk 9.Y — Diff view component

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies diff output schema and improvement counters
- [ ] Manual test: run two scans, eyeball the diff
- [ ] Step Report produced; user committed

---

## US-032: Revert a "Mark as done" action

**As** Maya
**I want** to revert a Pending Action I marked as done by mistake
**So that** my dashboard accurately reflects what is and is not actually fixed

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I have a Pending Action in completed state that I marked done by mistake
WHEN I click "Revert" with reason "I marked this done but the fix was not actually applied"
THEN PATCH /api/actions/{id} is called with `{ status: "revoked", reason: "..." }`
AND the action moves from completed to revoked state
AND the dashboard summary recounts (revoked actions do not count toward "completed" totals)

GIVEN an action is in revoked state
WHEN I view the action history
THEN I see the original completion timestamp, the revocation timestamp, and the revocation reason
AND the action is not re-surfaced as a fresh Pending Action (it remains revoked)

GIVEN the underlying drift event re-fires on the next cron run
WHEN the new drift event creates a fresh action
THEN the new action is independent (different action_id) and surfaces normally on the dashboard
```

### Why this matters

Trust. Marking something done is a commitment; the user needs an escape hatch when the commitment was wrong. Without revert, users will become afraid to click "Mark done" on actions they are not 100% sure about.

### Implementation notes (high level only)

- Add `revoked_at`, `revoked_reason` columns to `actions` (system-design §15.3).
- Update PATCH endpoint state machine.

### PLAN.md chunks generated by this story

- [ ] Chunk 9.X — Drizzle migration adding `revoked_at`, `revoked_reason`
- [ ] Chunk 9.Y — Revert button on completed action cards

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest state-machine test covers completed → revoked transition; rejects invalid transitions
- [ ] Manual test: mark done, then revert, verify dashboard counters update
- [ ] Step Report produced; user committed

---

## US-033: Try the public demo without signing up

**As** a hiring manager (or any casual reviewer) visiting auditpilot.dev for the first time
**I want** to click "Try the demo" and see a working dashboard without having to sign up or connect my own GitHub
**So that** I can understand what AuditPilot does in 90 seconds

### Acceptance criteria (Gherkin)

```gherkin
GIVEN I am on the auditpilot.dev landing page and have never signed up
WHEN I click "Try the demo"
THEN I am signed in as the demo user (`demo@auditpilot.dev`) within 2 seconds
AND I land on /dashboard?demo=true with a 64-control posture grid pre-populated with mixed statuses
AND a yellow banner reads: "This is the public demo. State is shared with all visitors. [Reset demo] [Sign up for your own account]"

GIVEN I am poking around the demo and the state is messy from a previous visitor
WHEN I click "Reset demo" in the banner
THEN POST /api/demo/reset is called
AND the demo data resets to seed in under 2 seconds
AND I see fresh seeded state

GIVEN multiple demo visitors arrive at the same time (rare; rate-limited to 30/min/IP)
WHEN they each interact with the demo
THEN they all see the same shared state
AND no per-visitor data isolation is performed (per ADR-0012)
AND a daily cron at 03:00 UTC auto-resets the demo regardless of button activity
```

### Why this matters

The demo is the highest-leverage UX surface for the public launch. A hiring manager who lands on auditpilot.dev and has to sign up before seeing anything is a bounced visitor. A working demo behind a single click is the difference between "I see what this does" and "I have no idea what this does."

### Implementation notes (high level only)

- ADR-0012 has the full design.
- System-design §14 is the architectural view.
- Seed fixture at `apps/api/seeds/demo_seed.sql`.

### PLAN.md chunks generated by this story

- [ ] Chunk 11.X — `apps/api/seeds/demo_seed.sql` hand-authored
- [ ] Chunk 11.Y — `POST /api/auth/demo` endpoint
- [ ] Chunk 11.Z — `POST /api/demo/reset` endpoint
- [ ] Chunk 11.AA — Demo banner component on dashboard
- [ ] Chunk 11.AB — Vercel Cron daily reset

### Definition of done

- [ ] All three acceptance criteria pass
- [ ] Auto test: Pytest verifies reset endpoint only resets the demo user (not real users)
- [ ] Auto test: rate limit on demo sign-in tested at 30/min
- [ ] Manual test: maintainer clicks "Try the demo" from a private window, verifies seeded experience
- [ ] Step Report produced; user committed

---

_Last updated: 2026-05-01. Cross-references: PRD §3 (JTBDs), §6 (features); SRS §2 (FRs); system-design.md §3 (sequence flows), §5 (API surface), §11–§15 (background jobs, LLM patterns, drift, demo, re-run); ADR-0001 through ADR-0012. Thirty-three stories cover every Must-tier feature in PRD §6.1 plus all three personas (Maya, Security Lead, AI Engineer) plus the casual-reviewer demo flow. Edge-case Gherkin scenarios in US-002, US-003, US-013, US-019. Should-tier features (Gmail/Slack/Calendar connectors) will get their own stories in v1.5._
