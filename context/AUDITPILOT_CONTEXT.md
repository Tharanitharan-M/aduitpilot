# AuditPilot Context: Strategic Brief

**Read this document first before writing a single line of code.** It contains every architectural decision, every constraint, and every "why." The companion `AUDITPILOT_FOUNDATIONS.md` covers domain knowledge (what SOC 2 is, what users do); this document covers strategic decisions (why this architecture, what to build, what not to build).

**Read order:**

1. This document (`AUDITPILOT_CONTEXT.md`) — strategy and decisions
2. `AUDITPILOT_FOUNDATIONS.md` — domain and user flow
3. `AUDITPILOT_TOOLING_LANDSCAPE.md` — tool-by-tool reference
4. `docs/adrs/0001-langgraph-runtime-choice.md`

---

## 1. What we're building

### 1.1 The product in one paragraph

**AuditPilot** is an open-source multi-agent SOC 2 readiness reference architecture. It connects read-only to GitHub, Gmail, Slack, and Calendar, runs three AI agents that detect compliance gaps, drafts policies, fills security questionnaires, and runs adversarial mock readiness challenges. It produces files that a founding engineer (Maya, our archetypal user) downloads — questionnaires, policies, gap reports — and suggestions she applies herself in her source tools. **AuditPilot is read-only on the way in, drafts and suggestions on the way out, never autonomous.**

### 1.2 What this is not

- **Not a Vanta competitor.** Vanta is at $300M ARR. We are an open-source reference architecture for the same problem space, not a product.
- **Not a CPA firm.** Per AICPA UPAct, only licensed CPA firms can issue formal `SOC 2 report` documents under SSAE No. 18 / AT-C 205. AuditPilot is a **readiness tool**, never an attestation tool.
- **Not autonomous.** Every fix is drafted, queued in a Pending Actions list, and applied by the human user in the source tool.

### 1.3 Strict language rules

Never use the words `audit`, `attest`, `certify`, or `SOC 2 report` without prefixing with `draft`, `readiness`, or `reference architecture`. Examples:

- ✅ "drafts SOC 2 readiness reports"
- ✅ "open-source reference architecture for SOC 2 readiness"
- ❌ "issues SOC 2 audits"
- ❌ "certifies SOC 2 compliance"

This is a legal shield, not a stylistic preference. AICPA and any CPA firm reviewing this project will care.

### 1.4 Project goals

AuditPilot serves two goals:

**Goal 1: Ship a production-grade reference architecture for the AI engineering community.** A Maya-shaped engineer should be able to fork the repo, run it on $0/month free tier, and learn what a real LangGraph 1.x + Pydantic AI + MCP system looks like end to end.

**Goal 2: Become a real open-source product others fork and use.** Realistic adoption ceiling: 1,500–3,000 stars in year one, modeled on comparable OSS compliance projects (Comp AI: ~1,400 stars after 14 months on $2.6M pre-seed).

When the two goals conflict, Goal 1 wins. In practice they rarely conflict — the architectural choices that demonstrate senior engineering also make the project more fork-able.

---

## 2. The architecture (decisions and rationale)

### 2.1 Stack at a glance

| Layer                                  | Tool                                                                   | Why                                                                                       |
| -------------------------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **Agent runtime**                      | **LangGraph 1.x**                                                      | Dominant production framework, broad industry adoption, no-breaking-changes commitment    |
| **Agent definitions**                  | **Pydantic AI**                                                        | Type-safe, dependency injection, same team as Pydantic v2                                 |
| **Type validation**                    | **Pydantic v2**                                                        | Spinal cord of the stack — FastAPI, MCP schemas, agent state, structured LLM outputs      |
| **Tool integration**                   | **MCP + 5 custom servers**                                             | Fastest-growing tool integration protocol; five authored servers is the headline artifact |
| **Cross-process protocol**             | **A2A v1.0**                                                           | One endpoint between AuditOrchestrator and AdversarialAuditor                             |
| **HITL pattern**                       | **LangGraph `interrupt()` + `Command(resume=)`**                       | Best-in-class HITL in 2026                                                                |
| **Backend framework**                  | **FastAPI**                                                            | Pydantic-native, async, OpenAPI auto-gen                                                  |
| **Frontend**                           | **Next.js 15** + **Vercel AI SDK 6** + **AI Elements** + **shadcn/ui** | The standard for 2026                                                                     |
| **LLM router**                         | **LiteLLM**                                                            | Multi-provider failover with hard daily caps                                              |
| **Default LLM**                        | **Gemini 2.5 Flash-Lite**                                              | Free tier, fast                                                                           |
| **Database**                           | **Neon Postgres + pgvector**                                           | Scale-to-zero, branching, free                                                            |
| **Object storage**                     | **Cloudflare R2**                                                      | 10GB + zero egress fees                                                                   |
| **Cache + rate limit**                 | **Upstash Redis**                                                      | Serverless-friendly free tier                                                             |
| **Auth**                               | **Clerk**                                                              | OAuth flows + pre-built Next.js components (10k MAU free)                                 |
| **Eval (general)**                     | **Promptfoo**                                                          | YAML in repo, GitHub Actions native                                                       |
| **Eval (RAG)**                         | **RAGAS**                                                              | Faithfulness, answer relevancy, context precision/recall                                  |
| **LLM observability**                  | **Langfuse**                                                           | OSS MIT, OTel-native, 50k events/mo free                                                  |
| **Error tracking + product analytics** | **PostHog**                                                            | Funnels, session replay, feature flags, frontend + backend error tracking                 |
| **Web analytics**                      | **Vercel Analytics**                                                   | Free with Hobby                                                                           |
| **Web vitals**                         | **Vercel Speed Insights**                                              | Free with Hobby                                                                           |
| **Backend metrics**                    | **Grafana Cloud Free**                                                 | OTel from FastAPI                                                                         |
| **Uptime + status**                    | **Better Stack**                                                       | Public status page                                                                        |
| **Frontend hosting**                   | **Vercel Hobby**                                                       | Free, Next.js native                                                                      |
| **Backend hosting**                    | **Cloud Run**                                                          | 360k vCPU-s/mo free                                                                       |
| **Cron jobs**                          | **Vercel Cron** (default) or **Cloud Run jobs**                        | Drift watcher every 6h                                                                    |
| **K8s (optional)**                     | **Oracle Cloud OKE Always Free**                                       | Helm chart for production users                                                           |
| **Local dev**                          | **Docker Compose**                                                     | One-command setup                                                                         |
| **License**                            | **Apache 2.0**                                                         | Permissive, enterprise-friendly                                                           |

**Total monthly cost: $0.** See `AUDITPILOT_TOOLING_LANDSCAPE.md` for tool-by-tool deep dive and alternatives we evaluated.

### 2.2 Why LangGraph and not ADK (the most important decision)

The earlier version of this plan used Google ADK 1.31.x as the runtime. We rejected it for three concrete reasons:

1. **Industry adoption.** LangGraph appears in roughly 25–30% of 2026 AI Engineer JDs and is the fastest-growing framework keyword. ADK appears in under 1% and is concentrated in Google ecosystem roles.

2. **Stability risk.** LangGraph 1.0 (October 2025) shipped with explicit no-breaking-changes commitment until 2.0. ADK 1.x had **31 minor releases in 12 months** with breaking changes between minors. ADK 2.0 is in Beta with breaking changes from 1.x and Google's own docs say "Do NOT use with ADK 1.x databases or sessions — they are incompatible." Building a 6-week portfolio on ADK means betting on framework stability that does not exist.

3. **Production proof points.** LangGraph has 30+ verified named external production deployments — Klarna ($60M savings claim), Uber, LinkedIn, JPMorgan, BlackRock ($11T AUM Aladdin Copilot), Replit, Cisco Outshift, Elastic, AppFolio, Vanta, plus more. ADK has roughly four — Comcast Xfinity Assistant, PayPal, Geotab, Genpact — plus Google's internal dogfood. The asymmetry is roughly 10:1 in independently-attested production breadth.

We kept A2A v1.0 as the cross-process protocol between AuditOrchestrator and AdversarialAuditor — that gives the protocol claim without the framework lock-in.

### 2.3 Why three agents and not eight (the second most important decision)

The earlier plan had eight agents (Orchestrator + Evidence Collector with 4 sub-agents + Control Mapper + Policy Drafter + Questionnaire Agent + Drift Watcher + Adversarial Auditor). We collapsed to three because every named authority in 2025–2026 published essays specifically warning against the eight-agent peer pattern.

**Anthropic, "Building Effective Agents" (Schluntz and Zhang, December 2024):** _"Consistently, the most successful implementations weren't using complex frameworks or specialized libraries. Instead, they were building with simple, composable patterns... we recommend finding the simplest solution possible and only increasing complexity when needed."_

**Cognition AI, "Don't Build Multi-Agents" (Walden Yan, June 2025):** _"in 2025, running multiple agents in collaboration only results in fragile systems. The decision-making ends up being too dispersed and context isn't able to be shared thoroughly enough between the agents."_ The April 2026 follow-up endorses single-writer with read-only specialist subagents — which is exactly our final design.

**OpenAI orchestration guide (2026):** _"Start with one agent whenever you can. Add specialists only when they materially improve capability isolation, policy isolation, prompt clarity, or trace legibility. Splitting too early creates more prompts, more traces, and more approval surfaces without necessarily making the workflow better."_

The three-agent design is:

1. **AuditOrchestrator (the only writer).** Owns the SOC 2 control list. Calls five MCP servers as tools to gather evidence (GitHub, Gmail, Slack, Calendar, plus the compliance-kb). Maps evidence to controls, drafts policies, fills questionnaires, generates gap reports. Single source of truth, single writer to LangGraph state.
2. **AdversarialAuditor (read-only critic).** Runs in a separate Cloud Run process for context isolation. Receives orchestrator output via A2A v1.0. Tries to find gaps a real reviewer would catch. Returns findings; the orchestrator decides what to do with them.
3. **HumanReviewGate (LangGraph `interrupt()`).** Approval surface for HITL. Maya reviews orchestrator drafts here, edits if needed, marks as done.

We still claim "multi-agent" honestly — there are two LLM-powered agents in two processes communicating via a real protocol. We still get parallel execution — the orchestrator fans out to four MCP servers concurrently for evidence collection. We still get separation of concerns — five MCP servers each own a focused responsibility. **The collapse from eight to three removes redundant LLM calls (token waste, latency, error accumulation) while preserving every architectural property that matters.**

### 2.4 The read-only-by-design principle (critical)

This is the legal and security spine of the project. Read it twice.

**AuditPilot reads your tools (GitHub, Gmail, Slack, Calendar) via read-only OAuth scopes. It never writes back to them.** Output is always one of two things:

1. **Files Maya downloads** — questionnaires (XLSX), policies (DOCX), gap reports (PDF/MD)
2. **Suggestions in a Pending Actions queue** — drafts of emails she sends herself, GitHub settings she flips herself, Slack messages she posts herself

When AuditPilot detects an issue (e.g., branch protection got disabled on `main`), it creates a card with the suggested fix, the link to the source tool, and a "Mark as done" button. Maya applies the fix. AuditPilot logs that she did.

**Why this matters:**

- **AICPA UPAct shield.** Compliance attestations require human professional judgment. An autonomous agent that "fixes" SOC 2 controls is a legal red flag.
- **Vanta does the same.** Their official SOC 2 product page says: _"Vanta connects read-only to your cloud, identity, code, and device tools."_ They generate _"remediation snippets so developers can resolve failing tests fast."_ Developers apply the snippet. Vanta does not.
- **Write OAuth scopes scare users.** Read-only OAuth dialogs are one-click; write dialogs slow approvals.
- **Bug blast radius.** A bug in an agent with write access to GitHub could disable security in production. Read-only removes that whole class of risk.

In every doc and every README: AuditPilot reads, drafts, suggests. The human applies.

### 2.5 Pydantic v2 is the spinal cord

Pydantic v2 is pervasive throughout the stack. Mention it explicitly in every architecture diagram and ADR:

- **FastAPI request/response models** — every endpoint has Pydantic input/output validation; FastAPI generates OpenAPI from these
- **MCP server tool schemas** — every tool we author has Pydantic-validated inputs and outputs (MCP uses JSON Schema, Pydantic generates JSON Schema)
- **LangGraph state** — `AuditPilotState` is a Pydantic model with typed fields for evidence, control_map, draft_policy, audit_findings, questionnaire_answers, drift_report
- **Pydantic AI agent definitions** — typed inputs, outputs, dependencies for the orchestrator and auditor
- **Structured LLM outputs** — when we ask Gemini for JSON (extracted control mappings, parsed questionnaire questions, drafted policies), we define a Pydantic model and force the LLM to comply
- **Cross-language type sharing** — Pydantic generates JSON Schema; our Zod schemas in TypeScript validate against the same JSON Schema; frontend and backend share types via OpenAPI

The discipline: typed end-to-end with Pydantic v2 throughout backend, agent state, MCP tool schemas, and structured LLM outputs; frontend Zod schemas validate against the same OpenAPI generated from Pydantic models.

### 2.6 The complete observability stack

This is one of the strongest senior-engineer signals in the project. Production SaaS teams in 2026 ship observability in sprint one, not sprint twelve. The standard combination is Datadog + PagerDuty for paid teams. We replicate the same coverage on $0/month with five free-tier tools across distinct layers:

| Layer                                               | Tool                              | Free tier                       | What it covers                                                                    |
| --------------------------------------------------- | --------------------------------- | ------------------------------- | --------------------------------------------------------------------------------- |
| LLM observability                                   | Langfuse Cloud Hobby              | 50k traces/month                | Agent traces, prompt versions, datasets, eval scoring                             |
| Error tracking + product analytics + session replay | PostHog Cloud Free                | 1M events, 5k replays/month     | Frontend + backend errors, funnels, retention, replay auto-correlated with errors |
| Backend metrics                                     | Grafana Cloud Free                | 10k series, 50 GB logs          | Cloud Run latency, throughput, error rate via OTel                                |
| Web analytics + vitals                              | Vercel Analytics + Speed Insights | Free with Hobby                 | Page views, referrers, LCP, FID, CLS, TTFB scored per page                        |
| Uptime + status page                                | Better Stack Free                 | 10 monitors, public status page | `status.auditpilot.dev` + downtime alerts                                         |

**PostHog is the single product + error tracking tool.** PostHog consolidated error tracking with auto-correlated session replays in 2025. When a JS or Python error fires, it appears inline in the PostHog session replay timeline. A reviewer can click any error and watch the user's actual session leading up to it — no context-switching between tools. For a single-tenant portfolio project, this eliminates the need for a separate error-tracking vendor.

**Frontend instrumentation specifically.** The Next.js app initializes three things in `instrumentation-client.ts`: PostHog client (errors + product analytics + session replay), Vercel Analytics (auto-injected via `<Analytics />`), and Vercel Speed Insights (auto-injected via `<SpeedInsights />`).

**Backend instrumentation specifically.** FastAPI initializes PostHog Python SDK (error tracking + server-side events), OpenTelemetry exporter pointed at Grafana Cloud, and Langfuse exporter for LLM-specific spans. LangGraph 1.x ships first-class OpenTelemetry support so agent steps automatically become OTel spans.

The discipline: comprehensive end-to-end observability across frontend (Vercel Analytics, Speed Insights, PostHog error tracking + session replay) and backend (OpenTelemetry, Grafana Cloud, Langfuse, PostHog Python SDK), with public status page on Better Stack. Auto-correlated errors-to-replays within PostHog. All free-tier.

### 2.7 Eval methodology (the discipline that signals senior)

We hand-label 100 cases for the gold set. We validate the LLM judge against 50 of those. We compute TPR (true positive rate), TNR (true negative rate), and Cohen's kappa. **If TPR/TNR drops below 0.85 or kappa drops below 0.7, we fix the rubric, not the metric.** Documented in `docs/evals/judge-validation.md`.

This judge-validation discipline is the rare move that signals senior production rigor. Most projects skip it. Hamel Husain calls it _"60–80% of development time on error analysis and evaluation"_ — this is the eval methodology that holds up under serious scrutiny.

We complement Promptfoo (YAML-driven, GitHub Actions native, 100-case gold set) with RAGAS specifically for the AuditOrchestrator's retrieval steps over the SOC 2 controls knowledge base. RAGAS gives us four RAG-specific metrics — faithfulness, answer relevancy, context precision, context recall — that catch retrieval quality regressions Promptfoo's generic LLM-as-judge would miss.

Continuous eval: every gap discovered by the AdversarialAuditor gets logged to a Langfuse dataset and added to the regression suite. **The project's evals get better over time.**

### 2.8 Why Clerk and not Supabase Auth

We are not using Supabase for the database (Neon) or storage (Cloudflare R2), so standalone Supabase Auth introduces a vendor for one feature. Clerk's pre-built auth components (`<SignIn />`, `<UserButton />`, `<OrganizationSwitcher />`) materially reduce frontend implementation work in Next.js 15 and speed up Sprint 3 delivery.

Trade-off accepted:

1. **Smaller free tier.** Clerk provides 10k MAU free vs Supabase Auth at 50k MAU.
2. **Higher per-MAU cost after free tier.** Clerk pricing scales faster than Supabase at high MAU.

At portfolio scale, both trade-offs are not material. The engineering-time savings are more valuable than the higher-volume pricing profile.

The discipline: six auth providers were evaluated (Clerk, Auth0, NextAuth, Firebase Auth, WorkOS, Supabase Auth). Clerk is chosen for this reference architecture because it removes frontend auth plumbing while fitting our actual usage constraints.

---

## 3. The 6-week build plan

This section is a one-page summary. **The authoritative build sequence lives in `PLAN.md`** at the project root, which breaks the 6 weeks into 11 sprints and ~70 small chunks (30–90 minutes each), with explicit acceptance criteria and dependency reasoning in `decisions/PLAN_JUSTIFICATION.md`. Keep `PLAN.md` and this section consistent — when they drift, `PLAN.md` is the source of truth.

The earlier framing of "4 weeks" was aspirational. Honest scope for a solo build at ~30 hours per week, including foundation documents and the launch sequence, is 6 weeks plus 2 weeks of buffer.

### Sprint shape (per `PLAN.md`)

| Week             | Sprint(s)     | Focus                                                                                                                                                            |
| ---------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0 (May 1–5)      | Sprint 0      | Documentation only — PRD, SRS, 9 ADRs, system design, user stories, repo scaffold                                                                                |
| 1 (May 6–12)     | Sprints 1, 2  | `compliance-kb-mcp` v0.1 buildable (publish deferred), backend skeleton + FastAPI ↔ LangGraph ↔ AI SDK 6 SSE bridge                                              |
| 2 (May 13–19)    | Sprints 3, 3.5, 4 | Clerk auth + GitHub OAuth, **connector-scoped repo picker (Sprint 3.5 per ADR-0015)**, AuditOrchestrator wired to UI, **`compliance-kb-mcp` v0.1.0 published to npm + PyPI** after orchestrator integration confirms the API |
| 3 (May 20–26)    | Sprints 5, 6  | Evidence collection + storage, HITL via `interrupt()`, policy drafting + DOCX export                                                                             |
| 4 (May 27–Jun 2) | Sprints 7, 8  | Questionnaire + SIG-Lite XLSX flow, AdversarialAuditor + A2A v1.0 cross-process                                                                                  |
| 5 (Jun 3–9)      | Sprints 9, 10 | Drift watcher + observability stack, eval suite + judge validation (TPR/TNR/Cohen's kappa)                                                                       |
| 6 (Jun 10–16)    | Sprint 11     | Launch polish — README + demo video + teardown blog + LinkedIn post + Show HN                                                                                    |
| 7–8 (Jun 17–30)  | Buffer        | Slipped sprints recover here.                                                                                                                                    |

### Why Sprint 1 builds but does not publish

Sprint 1 ships `compliance-kb-mcp` v0.1.0 buildable locally (tarball + wheel staged), but **publish to npm + PyPI is deferred to Sprint 4 chunk 4.7** so the orchestrator integration in Sprint 4 catches any API mistakes before the first public version ships. This avoids the version churn that comes from finding API problems after publish.

### Why all five MCP servers ship

The earlier draft considered cutting `policy-template-mcp` and `drift-watcher-mcp` to internal modules. The decision is closed — all five ship as published packages. The portfolio claim is "five open-source MCP servers (Apache 2.0, npm + PyPI)." Internal modules would weaken that claim.

### Cuts if behind schedule

If you fall behind, cut in this order:

1. ISO 27001 mapping (SOC 2 only)
2. Optional Helm chart for Oracle OKE (defer to v2 — the K8s deployment doc alone preserves the claim)
3. RAGAS retrieval metrics (Promptfoo + judge validation alone is sufficient)
4. Generative Trust Center page

**Never cut:** LangGraph + Pydantic AI core, all five MCP servers, Langfuse + Promptfoo with judge validation, the read-only-by-design model, the demo GIF, the YouTube video, the Show HN launch day decision.

---

## 4. The launch plan

### Phase 1 (Week 5 end): Public launch

- LinkedIn launch post + YouTube demo video + 3000–5000 word teardown blog
- Demo gated behind Clerk auth so anonymous users do not burn LLM quota
- Tag swyx, Hamel Husain, Eugene Yan thoughtfully (do not spam)
- Show HN on Tuesday or Wednesday morning ET with embedded video
- Product Hunt the following week
- DEV.to deep-dive two weeks after launch

### Phase 2 (Months 2–3): Compounding content

- Five SEO posts targeting: "open source SOC 2," "LangGraph multi-agent example," "Pydantic AI production architecture," "MCP servers for compliance," "agent reference architecture"
- Conference talk submissions: AI Engineer World's Fair (CFP for 2026 closed March 30, 2026 — submit for 2027), AI Engineer Europe
- Reach out to LangChain, Pydantic, Anthropic developer relations teams for potential boost / case study

---

## 5. The 2026 AI engineering landscape (research-backed)

This section explains why we made the choices we did. Use it when defending decisions in design reviews.

### 5.1 Framework signal in production (research from 534+ 2026 listings)

| Framework          | Industry adoption            | Trajectory                                        |
| ------------------ | ---------------------------- | ------------------------------------------------- |
| **LangChain**      | ~34% (#1)                    | Mature, steady                                    |
| **LangGraph**      | **~25–30%, fastest-growing** | Rising fast                                       |
| **MCP (protocol)** | ~17%, fastest-growing of all | Rising fast                                       |
| LlamaIndex         | 15–20%                       | Steady                                            |
| CrewAI             | 10–15%                       | Plateauing                                        |
| OpenAI Agents SDK  | 10–15%                       | Growing                                           |
| AutoGen            | ~10%                         | **Officially in maintenance mode since Sep 2025** |
| Semantic Kernel    | ~5%                          | Microsoft-only                                    |
| **Google ADK**     | **<1%**                      | Google ecosystem only                             |
| A2A protocol       | <2%                          | Rising                                            |
| **A2UI**           | **~0%** (zero presence)      | Not in market                                     |
| Pydantic AI        | <2% directly named           | Ubiquitous as dep                                 |

Frontier labs (Anthropic, OpenAI, Sierra) deliberately do NOT name frameworks in their JDs. They want engineers who pick frameworks fast. **What demonstrates production rigor is evals, tracing, error analysis, and deployed demos — not framework choice.** What demonstrates familiarity with the LangChain ecosystem is direct LangGraph experience.

---

## 6. Risks and mitigations

| Risk                                 | Mitigation                                                                                                                                                                           |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **6-week build slips**               | Cut order: ISO 27001 → optional Helm chart → RAGAS → Trust Center. Never cut: LangGraph + Pydantic AI core, the five MCP servers, evals, observability, the demo GIF                 |
| **Free-tier limit breach**           | Track Cloud Run vCPU-seconds, Langfuse events (50k/mo), Neon CU-h (100/mo). Generate budget alerts at 80% utilization. Worst case: ~$120/month if everything breaches simultaneously |
| **AICPA UPAct concerns**             | Strict language rules in 1.3. Read-only-by-design in 2.4. Never claim `audit`/`attest`/`certify` without `draft`/`readiness`/`reference architecture` prefix                         |
| **GitHub stars below expectation**   | Comp AI sets a realistic ceiling (~1,400 in 14 months on $2.6M raise). Anything above 500 stars in 12 months is a strong outcome                                                     |
| **LangGraph 2.0 ships during build** | Pin `langgraph>=1.0,<2.0`. LangGraph's no-breaking-changes commitment until 2.0 is explicit                                                                                          |
| **A2A v1.0 spec changes**            | Pin `a2a-sdk>=1.0,<2.0`. Advertise both v0.3 and v1.0 AgentCards as the spec recommends                                                                                              |

---

## 7. Things to never forget

These are non-negotiable rules that apply to every line of code, every commit message, every README sentence.

1. **AuditPilot is read-only on the way in. Drafts and suggestions on the way out. The human applies every fix.** Never write code that calls a write API (`POST` to GitHub, send Gmail, post Slack message). Read-only OAuth scopes only.
2. **LangGraph is the runtime. Pydantic AI defines agents. Pydantic v2 is everywhere.** Never import `google.adk`. Never import bare `langchain` for orchestration (use `langgraph` instead). Never skip Pydantic models for tool inputs/outputs.
3. **Three agents, not eight.** AuditOrchestrator, AdversarialAuditor, HumanReviewGate. Five MCP servers as tools. Never spawn additional LLM-powered agents without an ADR justifying why.
4. **Single writer to LangGraph state.** Only AuditOrchestrator writes. AdversarialAuditor reads orchestrator output, returns findings; orchestrator writes the response. This is Cognition's single-writer principle from the April 2026 multi-agent essay.
5. **Never use the words `audit`, `attest`, `certify`, or `SOC 2 report` without `draft`, `readiness`, or `reference architecture` prefix.** AICPA UPAct shield.
6. **Production-grade infrastructure means CI/CD, comprehensive testing, eval pipelines, observability dashboard, Docker Compose, optional K8s, security best practices.** Every deliverable hits the senior bar.
7. **License is Apache 2.0.** Permissive, enterprise-friendly. Never AGPLv3 (kills enterprise contributor pipeline).
8. **When generating code, prefer typed (Pydantic v2 / Zod) over loose schemas, async over sync, streaming over blocking, observable (OTel spans) over silent.** These are the senior-coded defaults.
9. **The story is the architecture decision, not the framework choice.** When in doubt, document the decision in an ADR.
