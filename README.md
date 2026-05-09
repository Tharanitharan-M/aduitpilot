# AuditPilot

An open-source multi-agent SOC 2 readiness reference architecture.

AuditPilot connects read-only to GitHub, Gmail, Slack, and Google Calendar, runs three AI agents that detect compliance gaps, draft policies, and fill security questionnaires. Everything it produces is a file you download or a suggestion you apply yourself. **It never writes back to your tools.**

> **Heads up:** AuditPilot is a readiness tool, not an attestation service. Only licensed CPA firms can issue formal SOC 2 reports under SSAE No. 18 / AT-C 205. AuditPilot helps you get ready for that process.

---

## What it does

1. Reads evidence from your connected tools (read-only OAuth scopes)
2. Maps evidence against SOC 2 Trust Services Criteria
3. Drafts policies, fills questionnaires, and generates gap reports
4. Queues suggestions in a Pending Actions list for you to review and apply

You apply every fix. AuditPilot logs that you did.

---

## Architecture

Three agents, not eight.

| Agent | Role |
|---|---|
| **AuditOrchestrator** | The only writer. Calls five MCP servers as tools, maps evidence to controls, drafts all outputs. |
| **AdversarialAuditor** | Read-only critic in a separate process. Finds gaps a real reviewer would catch, returns findings via A2A v1.0. |
| **HumanReviewGate** | LangGraph `interrupt()` surface. You review and approve before anything is finalized. |

Five custom MCP servers, all authored by us and published to npm and PyPI under Apache 2.0:

- `compliance-kb-mcp` — SOC 2 Trust Services Criteria knowledge base with hybrid pgvector + BM25 search
- `evidence-store-mcp` — typed read-only access to collected evidence
- `questionnaire-mcp` — SIG-Lite XLSX parser, question clustering, answer scaffolding
- `policy-template-mcp` — Markdown policy templates with control-citation slots
- `drift-watcher-mcp` — evidence snapshot diffing and drift event production

Plus four community MCP servers (forked, security-reviewed) for read-only OAuth integrations: GitHub (v1), Gmail (v1.5), Slack (v1.5), Calendar (v1.5).

### Stack

| Layer | Tool |
|---|---|
| Agent runtime | LangGraph 1.x |
| Agent definitions | Pydantic AI |
| Cross-process protocol | A2A v1.0 |
| Backend | FastAPI |
| Frontend | Next.js 15 + Vercel AI SDK 6 + shadcn/ui |
| LLM | Gemini 2.5 Flash-Lite via LiteLLM |
| Database | Neon Postgres + pgvector |
| Auth | Clerk |
| Storage | Cloudflare R2 |
| Cache | Upstash Redis |
| LLM observability | Langfuse |
| Error tracking + product analytics | PostHog |
| Backend metrics | Grafana Cloud (OTel) |
| Hosting | Vercel (frontend) + Cloud Run (backend) |

**Total monthly cost: $0.** Every tool listed has a free tier that covers portfolio-scale usage.

---

## Documentation

- [`context/AUDITPILOT_CONTEXT.md`](context/AUDITPILOT_CONTEXT.md) — strategy, decisions, and rationale
- [`context/AUDITPILOT_FOUNDATIONS.md`](context/AUDITPILOT_FOUNDATIONS.md) — SOC 2 domain knowledge and user flows
- [`context/AUDITPILOT_TOOLING_LANDSCAPE.md`](context/AUDITPILOT_TOOLING_LANDSCAPE.md) — tool-by-tool reference with alternatives evaluated
- [`docs/prd.md`](docs/prd.md) — product requirements
- [`docs/srs.md`](docs/srs.md) — software requirements
- [`docs/adrs/`](docs/adrs/) — fifteen architecture decision records covering runtime, agent count, HITL, observability, background jobs, prompt management, the public demo, the NIST 800-53 control catalog, the consolidated PostHog stack, and the connector-scoped repo picker
- [`docs/system-design.md`](docs/system-design.md) — full system design across fifteen sections: architecture, components, sequence flows, ERD, API surface, threat model (OWASP LLM Top 10), background jobs, LLM integration patterns, drift watcher, demo account, re-run/compare/revert flows
- [`docs/user-stories.md`](docs/user-stories.md) — thirty-three user stories in INVEST format covering all three personas (founding engineer, security lead, AI engineer) plus the casual-reviewer demo flow

---

## Status

Sprint 9 closed (2026-05-09). Sprints 0–9 are done; Sprint 10 (eval suite + judge validation + prompt management) is next.

What works end-to-end today:

- Sign up / sign in / sign out (Clerk).
- Connect GitHub via read-only OAuth (`public_repo`, `read:org`); disconnect at will.
- Repo-picker step between connect and first scan: choose which repos AuditPilot is allowed to read. Default-deny — nothing is selected unless the user picks it. Selection is persisted on the connector and editable from the dashboard.
- Orchestrator refuses to start a `run_readiness_scan` against an empty repo scope before any LLM call (ADR-0015).
- Click "Run readiness scan" — the orchestrator streams tool calls live via AI SDK 6 SSE, maps evidence to SOC 2 TSC clauses backed by NIST 800-53 controls, and renders tool cards for each lookup.
- SOC 2 Trust Services Criteria posture grid with control drill-down panel (status, NIST refs, evidence list, related actions).
- Pending Actions queue with an approve / reject / mark-done / revert state machine. Revert flips a completed action back to revoked with a required reason.
- Policies workspace: render IRP, Access Control, Change Management, Vendor Management with live citation slots from the user's own scan.
- Questionnaire workspace: drag-drop a SIG-Lite XLSX, get a draft fill grouped by domain with citation pickers and inline editing.
- Mock readiness challenge: orchestrator hands the scan to the AdversarialAuditor over A2A v1.0; findings + Markdown gap report download.
- Drift watcher: Vercel Cron every 6 hours diffs evidence projections, surfaces real configuration changes (not cosmetic noise) with 2-scan flap protection. Resolve / dismiss with reason from the dashboard.
- Scan runs page: re-run any past scan inheriting the user's current scope; pick two and compare side-by-side with regression / improvement coloured cells.
- Cancel-token: closing the browser mid-scan stops the backend stream gracefully.
- `compliance-kb-mcp` v0.2.0 published to [PyPI](https://pypi.org/project/compliance-kb-mcp/) and [npm](https://www.npmjs.com/package/@auditpilot/compliance-kb-mcp). `evidence-store-mcp`, `policy-template-mcp`, `questionnaire-mcp`, `drift-watcher-mcp` v0.1.0 ready for publish — five of five MCP servers under Apache 2.0.

Test counts at end of Sprint 9: 254 + 23 + 25 + 25 + 23 + 21 + 36 = **407 pytest** plus **102 vitest**; `tsc --noEmit` clean, ruff clean. (One pre-existing Sprint 7 storage-mock flake on `test_handler_pipeline_marks_ready`.)

Full build runs May through July 2026 across eleven sprints. Public demo URL target: July 1, 2026.

---

## Local development

There are two supported workflows. Both bring up the FastAPI backend on `:8000` and the Next.js frontend on `:3000` together.

### Native (fast iteration, no Docker)

```bash
# 1) clone + bootstrap deps
git clone https://github.com/tharani/auditpilot.git
cd auditpilot
make install

# 2) configure secrets (Clerk + PostHog + LLM keys)
cp apps/api/.env.example apps/api/.env       # then fill in
cp apps/web/.env.example apps/web/.env.local # then fill in

# 3) start FastAPI + Next.js together (Ctrl-C stops both)
make dev

# verify both services
make health
# FastAPI:  {"status":"ok",...}
# Next.js:  up

# stop everything
make stop

# run the full verify gauntlet (typecheck + vitest + pytest)
make verify
```

### Docker (full stack including Postgres + Redis)

```bash
make docker-up    # build + start postgres + redis + api + web
make docker-logs  # tail
make docker-down  # tear down
```

See [`Makefile`](Makefile) for the complete target list (`make help`).

---

## Design decisions

Major decisions are documented as ADRs in [`docs/adrs/`](docs/adrs/). Quick summary:

- **LangGraph over Google ADK** — LangGraph appears in ~25–30% of 2026 AI engineering job listings and ships with a no-breaking-changes commitment through 2.0. ADK has under 1% industry adoption and had 31 minor releases with breaking changes in 12 months. (ADR-0001)
- **Three agents over eight** — Backed by Anthropic's "Building Effective Agents," Cognition AI's single-writer principle, and OpenAI's orchestration guide. Fewer agents means less token waste, faster traces, and smaller error blast radius. (ADR-0002)
- **Read-only by design** — Read-only OAuth scopes only. This is both a legal decision (AICPA UPAct) and a product one. Vanta, the leading commercial product in this space, works the same way. (ADR-0004)
- **Repo selection at scan time** — After connect, the user picks which repos to scan from a default-deny picker. The selection is persisted on the connector, drives every scan and re-run, and the orchestrator refuses to start with an empty scope. The user controls **which** reads happen, not just **how**. (ADR-0015)
- **Clerk over standalone Supabase Auth** — we already use Neon for database and Cloudflare R2 for storage, so Clerk avoids carrying Supabase for a single feature while giving pre-built Next.js auth components (`<SignIn />`, `<UserButton />`, `<OrganizationSwitcher />`). The 10k MAU free tier covers portfolio scale. (ADR-0008)
- **Redis Streams for background jobs** — Real queue semantics (consumer groups, ACK, dead-letter) on infrastructure we already pay zero for. Kafka would have been over-engineering at this scale. (ADR-0010)
- **Langfuse-backed prompt management with local fallback** — YAML in repo as source of truth, pushed to Langfuse on deploy, runtime fetch with 60-second cache and local fallback on outage. (ADR-0011)
- **Public demo account with shared state** — One demo account with seeded synthetic data, a Reset button, and a daily auto-reset cron. Casual visitors see a working dashboard in 90 seconds without sign-up. (ADR-0012)

---

## License

[Apache 2.0](LICENSE)
