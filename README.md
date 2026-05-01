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

Five MCP servers handle tool integration: `github-mcp`, `gmail-mcp`, `slack-mcp`, `calendar-mcp`, and `compliance-kb-mcp`. All published to npm and PyPI under Apache 2.0.

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
| Auth | Supabase Auth |
| Storage | Cloudflare R2 |
| Cache | Upstash Redis |
| LLM observability | Langfuse |
| Error tracking | Sentry |
| Product analytics | PostHog |
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
- [`docs/adrs/`](docs/adrs/) — nine architecture decision records covering runtime, agent count, HITL, observability, and more

---

## Status

Currently in Sprint 0 (week of May 1, 2026): documentation, ADRs, and repo scaffold.

Full build runs May through mid-June 2026 across 11 sprints. See [`PLAN.md`](PLAN.md) for the complete sprint breakdown.

---

## Local development

> Coming in Sprint 1. Docker Compose one-command setup.

```bash
# Clone the repo
git clone https://github.com/tharani/auditpilot.git
cd auditpilot

# Copy environment variables
cp .env.example .env.local

# Start everything
docker compose up
```

---

## Design decisions

Major decisions are documented as ADRs in [`docs/adrs/`](docs/adrs/). Quick summary:

- **LangGraph over Google ADK** — LangGraph appears in ~25–30% of 2026 AI engineering job listings and ships with a no-breaking-changes commitment through 2.0. ADK has under 1% industry adoption and had 31 minor releases with breaking changes in 12 months.
- **Three agents over eight** — Backed by Anthropic's "Building Effective Agents," Cognition AI's single-writer principle, and OpenAI's orchestration guide. Fewer agents means less token waste, faster traces, and smaller error blast radius.
- **Read-only by design** — Read-only OAuth scopes only. This is both a legal decision (AICPA UPAct) and a product one. Vanta, the leading commercial product in this space, works the same way.
- **Supabase Auth over Clerk** — 50k MAU free (vs. Clerk's 10k), MFA included free (vs. Clerk's $100/mo add-on), and MFA is a SOC 2 control we need to demonstrate.

---

## License

[Apache 2.0](LICENSE)
