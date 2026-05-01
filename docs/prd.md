# AuditPilot — Product Requirements Document

**Status:** Draft | **Version:** 0.1 | **Date:** 2026-05-01
**Reviewers:** open for community feedback

> This is an open-source reference architecture. Nothing in this document constitutes legal, accounting, or compliance advice. AuditPilot is a readiness tool, not a CPA firm, and cannot issue any form of attestation, certification, or SOC 2 report.

---

## Section 1: Problem Statement

Every B2B software company eventually gets the question from a prospective customer: "Do you have SOC 2?" SOC 2 readiness — the process of implementing the controls that a licensed CPA firm later verifies — takes a typical startup six to eighteen months of engineering time and costs forty thousand to one hundred twenty thousand dollars in software platform fees alone, before paying the external auditor's bill. The process is painful not because the underlying security controls are hard to build, but because the evidence is scattered: branch protection rules live in GitHub, multi-factor enforcement lives in an identity provider, access review logs live in a spreadsheet, and vendor agreements live in someone's inbox. Pulling all of that together, mapping it to sixty-four Trust Services Criteria controls, drafting the policies that document what you do, and filling out security questionnaires that customers send — all of that work lands on one or two engineers who are also shipping product. The overhead is high, the tooling is expensive, and the feedback loop is slow.

Commercial platforms — Vanta, Drata, Secureframe, Sprinto, Scrut, and a growing list of newer entrants — solve the connectivity problem well. They integrate with hundreds of cloud tools, continuously collect evidence, and map it to SOC 2 criteria automatically. By 2025, most have added AI features: questionnaire answer drafting, policy summarization, gap detection between written policy and live configuration. These are genuinely useful capabilities. But they share three structural limitations that no commercial vendor has resolved. First, pricing starts at seven thousand five hundred dollars per year for the smallest startups and scales well past one hundred thousand dollars for mid-market companies, which means the earliest-stage teams — the ones who most need automated help — are priced out. Second, every commercial platform is a closed system. You cannot read the prompts, inspect the agent graph, trace a reasoning step, or verify how the AI reaches a conclusion. When the platform tells you a control passes, you have no way to examine the evidence chain. Third, and most importantly, none of these platforms publishes an eval harness. No TPR, no TNR, no Cohen's kappa, no documented failure-mode taxonomy. The AI quality claims are unverifiable.

On the open-source side, the picture is thinner than it looks. `strongdm/comply` (markdown-based SOC 2 pipeline) was abandoned in 2021. `getprobo/probo` and `theopenlane/core` are actively maintained workflow management tools — task lists and evidence upload forms — with no LLM integration. `trycompai/comp` launched in January 2025 and is the most serious open-source competitor, but its AI internals are undocumented and its license is AGPLv3 (incompatible with most commercial forks). No existing open-source tool uses LangGraph 1.x or Pydantic AI as its runtime. No tool implements an adversarial auditor agent that takes the stance of a skeptical reviewer, challenges individual control claims, and produces written objections a founding engineer can learn from. No tool makes human-in-the-loop a first-class safety constraint documented at the architecture level rather than a checkbox in a settings menu. And no tool publishes an eval pipeline with a judge-validation methodology so the community can verify AI output quality independently. That is the gap AuditPilot fills: a production-grade, Apache 2.0, multi-agent reference architecture for SOC 2 readiness, with a documented LLM pipeline, a published eval harness, and an adversarial readiness challenge — all built on frontier tooling (LangGraph 1.x, Pydantic AI 1.0, MCP 2025-11-25) so the codebase is a learning artifact for every AI engineer who forks it.

---

## Section 2: Users

AuditPilot targets three distinct user types. All three interact with the same open-source codebase, but each uses it differently and has different success criteria.

### 2.1 The Founding Engineer at an Early-Stage Startup (Primary)

A founding engineer at a pre-Series B startup is typically the first or second engineer on the team and is responsible for both shipping product and maintaining infrastructure security. They receive a vendor security questionnaire from a prospective enterprise customer and have no dedicated security person to hand it to. They know they need SOC 2 eventually but have never been through the process and do not know which controls they already satisfy versus which ones require new work. They have a GitHub organization with reasonable security hygiene — branch protection, some MFA enforcement — but no documented policies, no evidence collection process, and no budget for a ten-thousand-dollar-per-year compliance platform. They learn by reading well-documented open-source code and want a system they can run locally, inspect end to end, and adapt to their specific stack.

**Three jobs this user brings to AuditPilot:**
1. Know, within an hour of connecting GitHub, which SOC 2 Trust Services Criteria controls they already satisfy and which ones are gaps.
2. Download a draft security policy they can review, edit, and adopt without writing from a blank page.
3. Submit a filled vendor security questionnaire within a day rather than a week.

---

### 2.2 The Security Lead at a Scaling Startup (Secondary)

A security lead or Head of Security at a thirty-to-two-hundred person Series A or B company has a compliance platform budget but is dissatisfied with the black-box nature of the AI features their current platform offers. They have been through one SOC 2 Type I readiness review and are now preparing for Type II. They want to understand how an AI system reasons about their control posture, not just receive a pass/fail verdict. They are evaluating open-source alternatives they can self-host so that evidence never leaves their environment. They have a small team — one or two security engineers — who can read Python and TypeScript and would contribute back to an OSS project that solves their problem.

**Three jobs this user brings to AuditPilot:**
1. Run an adversarial mock readiness challenge that surfaces weak controls before a real external reviewer does.
2. Review AI-generated control mapping decisions with full reasoning traces (Langfuse spans) rather than opaque scores.
3. Self-host the entire pipeline inside their VPC so evidence data does not leave their environment.

---

### 2.3 The AI Engineer Using This as a Reference Architecture (Tertiary)

An AI engineer, ML platform engineer, or software engineer learning the multi-agent AI space wants a production-grade, real-world example of a LangGraph + Pydantic AI system that is more complex than a toy chatbot but scoped enough to understand in a day. They are not going through SOC 2 readiness themselves. They want to understand how orchestrator-adversarial-HITL agent topologies work, how MCP servers are published and consumed, how an eval pipeline with judge validation is wired together, and how SSE streaming works between a FastAPI backend and a Next.js 15 frontend. They read the code, the ADRs, and the eval methodology. They may contribute a new connector, a new framework mapping, or a new eval case.

**Three jobs this user brings to AuditPilot:**
1. Read a codebase that demonstrates LangGraph 1.x + Pydantic AI 1.0 in a real, non-trivial application.
2. Fork the MCP server pattern and adapt it for a different domain (healthcare compliance, financial controls, GDPR).
3. Understand how to wire Promptfoo + a judge-validation script (TPR/TNR/Cohen's kappa) into a CI pipeline for an LLM-powered system.

---

## Section 3: Jobs to Be Done

Jobs are written in the canonical `When I'm X, I want Y, so I can Z` form. Each job maps to at least one user type from Section 2.

| # | When I am... | I want... | So I can... | Primary user |
|---|---|---|---|---|
| JTBD-1 | preparing a startup for a first SOC 2 readiness review | to see, within one session, which of the 64 Trust Services Criteria controls I satisfy and which are gaps | prioritize engineering work and stop guessing at coverage | Founding engineer |
| JTBD-2 | receiving a vendor security questionnaire (SIG-Lite, CAIQ) from a prospective customer | to get a pre-filled draft with citations to my existing controls and policies | review and submit within a day instead of a week | Founding engineer / Security lead |
| JTBD-3 | drafting a security policy I have never written before | to get an AI-authored template grounded in my actual control posture | review and approve rather than author from a blank page | Founding engineer / Security lead |
| JTBD-4 | preparing for a real external readiness review | to run an adversarial agent that challenges my evidence and flags weak controls | discover gaps before a live reviewer does | Security lead |
| JTBD-5 | evaluating AI quality claims in a compliance tool | to see published eval metrics (TPR, TNR, Cohen's kappa) with a documented methodology | trust the system's outputs without guessing at quality | Security lead / AI engineer |
| JTBD-6 | learning how to build multi-agent AI systems | to read a production-grade reference implementation using LangGraph 1.x, Pydantic AI, and MCP | avoid months of architecture research and start from a validated pattern | AI engineer |
| JTBD-7 | detecting that a control has drifted out of compliance after a deployment | to receive a Pending Action card with the specific setting to fix and a direct link to the source tool | apply the fix myself in the right place without hunting through dashboards | All users |

---

## Section 4: Success Metrics

Every metric has a target number and a target date. Metrics marked `X%` will be filled in after Sprint 10 evals run — no fabricated numbers.

### 4.1 Open-source adoption metrics

| Metric | Target | Date |
|---|---|---|
| GitHub stars | 500 | 2026-10-01 |
| GitHub stars | 1,500 | 2027-04-01 |
| `compliance-kb-mcp` weekly downloads on npm | 1,000/week | 2027-01-01 |
| `compliance-kb-mcp` weekly downloads on PyPI | 500/week | 2027-01-01 |
| External contributors (non-owner PRs merged) | 3 | 2026-10-01 |
| Show HN posts that reach top 10 | 1 | 2026-07-15 |

### 4.2 Technical quality metrics

| Metric | Target | Date | Notes |
|---|---|---|---|
| Control-mapping TPR | X% | Sprint 10 | Replace placeholder after eval run |
| Control-mapping TNR | X% | Sprint 10 | Replace placeholder after eval run |
| Judge Cohen's kappa | X (≥ 0.70 required) | Sprint 10 | Replace placeholder after eval run |
| RAGAS faithfulness | X (≥ 0.80 required) | Sprint 10 | Replace placeholder after eval run |
| P50 readiness scan latency (wall clock, GitHub connector) | ≤ 30 seconds | Sprint 4 demo day | Measured on local Docker Compose |
| AICPA UPAct violations in external-facing copy | 0 | Ongoing | compliance-language-guard blocks CI |
| Promptfoo eval regression threshold | 0 regressions > 2% | Every PR | eval-runner sub-agent enforces |

### 4.3 Narrative milestones

| Milestone | Target date |
|---|---|
| Demo video published on YouTube (5 min) | 2026-07-01 |
| Blog post published (3,000–5,000 words) | 2026-07-01 |
| AuditPilot live at public URL (`auditpilot.dev`) | 2026-07-01 |
| Conference talk submitted (AI Engineer World's Fair 2027 CFP) | 2027-01-01 |

---

## Section 5: Non-Goals

Non-goals are first-class requirements. They prevent scope creep and protect against AICPA UPAct liability. Each non-goal has a reason.

| # | Non-goal | Reason |
|---|---|---|
| NG-1 | **Not a Vanta competitor.** AuditPilot is an open-source reference architecture, not a commercial SaaS product. It will never have a pricing page, a sales team, or an uptime SLA for paying customers. | Positioning this as a Vanta competitor is a false claim that would mislead both engineers evaluating the project and any potential employer. |
| NG-2 | **Not a CPA firm. Cannot produce attestations, certifications, or any form of draft SOC 2 report that a licensed CPA firm would issue.** Only licensed CPA firms can produce SOC 2 readiness opinions under SSAE No. 18 / AT-C 205. AuditPilot produces internal readiness assessments and draft outputs for human review. | AICPA UPAct (Uniform Practice Act) imposes civil and criminal liability for unlicensed practice of accountancy. This is a hard legal constraint, not a stylistic preference. |
| NG-3 | **No autonomous remediation. No write API calls.** AuditPilot will never call a write endpoint on GitHub, Gmail, Slack, Calendar, or any other source tool. Every fix is a Pending Action that the human applies in the source tool. | Write-access agents introduce a whole class of production-safety risk. Read-only OAuth scopes are simpler to approve and create zero liability if the agent makes an error. |
| NG-4 | **No auto-fix of security controls.** Even where the fix is mechanical (turn on branch protection, enforce MFA), AuditPilot provides the link and the instruction — the human clicks. | Compliance requires human judgment and documented human action. An agent that auto-flips settings would be a red flag to any security director and would fail most auditor questionnaires about change management. |
| NG-5 | **Not a multi-framework GRC platform in v1.** Version 1 covers SOC 2 Trust Services Criteria (64 controls, CC1–CC9) only. ISO 27001, HIPAA, PCI-DSS, and GDPR mappings are deferred to v2. | Breadth before depth is the wrong tradeoff for a reference architecture. One framework done well is more valuable to engineers than six frameworks done poorly. |
| NG-6 | **Not an evidence archive for actual external readiness reviews.** AuditPilot collects evidence to generate readiness recommendations. It is not a certified evidence vault that a licensed CPA can rely on directly. | This would require SOC 2 Type II on AuditPilot itself, legal agreements with CPA firms, and an entirely different product scope. |
| NG-7 | **Not a compliance training platform.** AuditPilot does not teach engineers what SOC 2 controls mean or how to implement them from scratch. It assumes the user has basic security hygiene already in place and wants to measure and document it. | Training is a solved problem (SANS, Secureframe Academy, Vanta Learning). Adding training would bloat scope with no architectural value. |
| NG-8 | **No Oracle OKE Helm chart in v1.** Kubernetes deployment is deferred to v2. Cloud Run + Vercel is sufficient for the demo and for fork-and-run use cases. | Time-boxed at 6 weeks. The OKE Helm chart is a resume embellishment that costs two days of Sprint time and adds no user-facing value in v1. |

---

## Section 6: Feature List — v1 (Must / Should / Won't)

Features are grouped by Must (ship with v1), Should (ship if time allows in v1), and Won't (explicitly deferred or excluded). Every Must feature references at least one user story ID from Sprint 0E. User stories are detailed in `docs/user-stories.md`.

### 6.1 Must — v1 ships with these

| Feature | Description | User story IDs |
|---|---|---|
| **GitHub read-only connector** | OAuth authorization with `repo:read` and `org:read` scopes. Reads branch protection rules, MFA enforcement, code scanning status, secret scanning alerts, and dependabot config. | US-001, US-002 |
| **Automated control evidence collection** | AuditOrchestrator reads GitHub evidence and maps it to SOC 2 TSC criteria using the compliance-kb-mcp server. Produces a structured control map with gap flags. | US-003, US-004 |
| **Control posture grid** | Dashboard component showing all 64 Trust Services Criteria controls as green / yellow / red, updated in real time as the orchestrator streams results. | US-006 |
| **Pending Actions queue** | Every gap the orchestrator detects creates a Pending Action card with the specific setting to change, a direct link to the source tool, and a "Mark as done" button the user clicks after applying the fix manually. | US-007 |
| **Draft policy generation** | AuditOrchestrator drafts Markdown security policies (Incident Response, Access Control, Change Management, Vendor Management) grounded in actual control posture and compliance-kb citations. | US-011, US-012 |
| **Human review gate (HITL)** | LangGraph `interrupt()` node pauses the orchestrator before any policy draft or gap report is surfaced. The human reviews in the Pending Actions queue and must explicitly approve, edit, or reject before the workflow proceeds. | US-013 |
| **SIG-Lite questionnaire auto-fill** | User uploads a SIG-Lite XLSX. AuditOrchestrator clusters questions, retrieves relevant evidence, and drafts answers with citations. User downloads the filled XLSX and reviews before submitting to the vendor. | US-016, US-017 |
| **Adversarial mock readiness challenge** | AdversarialAuditor agent takes the stance of a skeptical external reviewer, challenges the orchestrator's control claims, produces written objections, and surfaces gaps the orchestrator missed. Results flow into the gap report. User triggers this explicitly — it never runs automatically. | US-019, US-020 |
| **Langfuse trace observability** | Every orchestrator invocation emits OTel spans to Langfuse. Users can inspect the full reasoning trace, tool calls, and token costs for any session. | US-008 |
| **Promptfoo eval suite with judge validation** | 100-case gold set (hand-labeled). CI gate on control-mapping accuracy. Judge validation script outputs TPR, TNR, and Cohen's kappa. Results published to `docs/evals/`. | US-009 |
| **Supabase Auth (email + GitHub OAuth)** | Users sign up, log in, and connect GitHub via Supabase Auth. Sessions persist via HTTP-only cookies. No custom auth code. | US-001 |
| **SSE streaming chat interface** | Next.js 15 + Vercel AI SDK 6 `useChat` hook streams orchestrator messages, tool call cards, and control posture updates in real time. | US-005, US-010 |

### 6.2 Should — ship in v1 if Sprint velocity allows

| Feature | Notes |
|---|---|
| Gmail read-only connector | Reads vendor agreements, access review emails, and security notification threads. Requires additional OAuth scope. |
| Slack read-only connector | Reads security-incident channels and access-review threads. |
| Calendar read-only connector | Detects overdue access reviews by scanning calendar invites. |
| Docker Compose one-command local dev | `docker compose up` brings up Postgres, Redis, FastAPI, and Next.js with hot reload. Lets any engineer fork and run in under ten minutes. |

### 6.3 Won't — v1 explicitly excludes these

| Feature | Reason |
|---|---|
| Write API calls to any source tool | NG-3. Hard constraint. Never in any version. |
| Draft readiness opinions or any output a CPA firm would issue (internal readiness gap reports are fine; external attestation documents are not) | NG-2. AICPA UPAct. Never. |
| ISO 27001 / HIPAA / PCI-DSS mappings | NG-5. Deferred to v2. |
| Autonomous policy publication | NG-3, NG-4. Policies are downloaded; user publishes. |
| Penetration testing or vulnerability scanning | Different product category. Out of scope. |
| Oracle OKE Helm chart | NG-8. Deferred to v2. |
| Generative Trust Center page | Nice-to-have for later. No user story. |
| Commercial SaaS pricing, SLAs, or support contracts | NG-1. This is an open-source reference architecture. |

---

_Last updated: 2026-05-01. Update Section 4.3 metric placeholders after Sprint 10 eval run._
