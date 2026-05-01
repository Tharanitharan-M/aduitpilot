# ADR-0011: Langfuse-Backed Prompt Management with Local Fallback

**Date:** 2026-05-01
**Status:** Accepted
**Deciders:** AuditPilot maintainers
**Refs:** ADR-0006, ADR-0009; system-design.md §12; PLAN.md Sprint 4, Sprint 10

---

## Context and Problem Statement

AuditPilot's three LLM-powered agents (AuditOrchestrator, AdversarialAuditor, PolicyDrafter step inside orchestrator) run on prompts that:

- Need versioning so a regression in eval scores can be tracked to a specific prompt change
- Need to be deployable independently of code releases (a prompt fix should not require a Cloud Run redeploy)
- Need to be reviewable by humans without reading Python source
- Need to be portable so a fork running self-hosted can use the same prompts
- Need to fail closed when the prompt-management backend is unreachable (the agent must not silently use a stale or wrong prompt)

The naive option is to embed prompts as Python f-strings inside `apps/api/agents/orchestrator.py`. This locks prompts to code releases, makes them invisible to non-engineers, and makes prompt versioning a code-history exercise rather than a first-class concern.

Langfuse (already chosen for LLM observability per ADR-0009) ships a Prompt Management feature with version-controlled prompts, environment labels, and a fetch API. The question is whether to use it as the source of truth, use a local file as the source of truth, or build a hybrid.

---

## Decision

**Hybrid model: prompts are version-controlled in the repo as the source of truth, pushed to Langfuse on deploy, and fetched at runtime from Langfuse with a local file as the failsafe fallback.**

Concretely:

1. **Source of truth:** YAML files at `apps/api/agents/prompts/<agent>/<version>.yaml`. Committed to git. Reviewed in PRs.
2. **Push on deploy:** A CI step uploads any new or modified YAML to Langfuse on every merge to `main`, tagging the new version with the `production` label.
3. **Runtime fetch:** `apps/api/agents/prompts/loader.py` calls `langfuse.get_prompt("orchestrator", label="production")` on each agent invocation, with a 5-second timeout and a 60-second in-memory cache.
4. **Local fallback:** If the Langfuse fetch fails (timeout or 5xx), the loader falls back to the local YAML file shipped with the container. The fallback emits a Sentry warning so the operator notices the Langfuse outage.
5. **Prompt evals:** The Promptfoo eval suite tests the YAML files directly (the source of truth), not the Langfuse-fetched copy. This guarantees that what is in the repo matches what is evaluated.

---

## Rationale

### Why have a backend at all (vs. only YAML in repo)

A YAML-only approach (read prompt from disk on each agent invocation) is the simplest possible design. It works. The reasons against:

1. **No runtime control.** Fixing a bad prompt requires a code redeploy. Cloud Run cold-start cost on the redeploy is 1–3 seconds of user-facing latency for the next request after rollout.
2. **No A/B testing.** Two prompt versions cannot run in parallel for measurement without writing custom traffic-splitting code.
3. **No non-engineer surface.** A subject-matter reviewer (e.g. a CPA who consults on the project) cannot inspect or comment on prompts without reading raw YAML in a GitHub PR.
4. **No prompt-trace linking.** A Langfuse trace cannot link directly to "which prompt version produced this output" if the prompt only exists in code.

These costs are real but small at v1.0. The pure-YAML approach would be acceptable. Langfuse adds enough value to justify the extra integration step.

### Why have a repo-side source of truth (vs. only Langfuse)

A Langfuse-only approach (no YAML files; prompts edited only via Langfuse UI) is also simple. The reasons against:

1. **No diff history in PR review.** A change to a prompt is a 50-line system-prompt edit. Review in the Langfuse UI is not the same as review in a GitHub PR with file diffs and comments.
2. **No CI test path.** Promptfoo evals run in CI; CI cannot reach Langfuse Cloud reliably from every PR without exposing API keys to forks.
3. **Lost on Langfuse outage.** If Langfuse Cloud is unavailable, there is no source of truth — the prompts simply do not exist for the duration of the outage.
4. **No fork portability.** A fork must either re-author every prompt in their own Langfuse instance or pull prompts from a shared Langfuse account (which adds an account-management problem).

The hybrid resolves these. The YAML files are the canonical artifact; Langfuse adds versioning, deployment, and runtime mutability.

### Why YAML (not Markdown, not Python, not JSON)

| Format | Diffability | Schema validation | Comments | Multiline literals |
|---|---|---|---|---|
| YAML | Good (line-based) | Yes (via Pydantic) | Yes | Yes (`|` block scalars) |
| Markdown | Excellent | No | Yes | Yes |
| Python | Good | Yes | Yes | Awkward (triple-quoted strings) |
| JSON | Bad (no comments) | Yes | No | No |

YAML wins for this use case: line-based diffs in PR review, Pydantic-validated schema, comments allowed for inline reviewer notes, block scalars for multi-paragraph system prompts.

### Schema for a prompt YAML file

```yaml
# apps/api/agents/prompts/orchestrator/v3.yaml
name: orchestrator
version: 3
model: gemini-2.5-flash-lite
temperature: 0.0
max_tokens: 4096
system: |
  You are AuditOrchestrator. Given evidence collected from a user's
  source tools, map each evidence item to SOC 2 Trust Services
  Criteria control IDs and decide its status.
  ...
tool_definitions:
  - name: compliance-kb-mcp.search_controls
    description: Search controls by query, framework, and k
    schema_ref: packages/compliance-kb-mcp/schemas/search_controls.json
guardrails:
  - delimiter_evidence: true
  - max_turns: 10
  - cost_cap_usd: 0.10
few_shot:
  - input: |
      Evidence: branch protection on main: {require_pr_reviews: true}
    output: |
      [{"control_id": "CC8.1", "status": "PASSING", "confidence": 0.92, ...}]
metadata:
  created_at: 2026-05-01
  reason_for_change: |
    v2 had cases where the model invented control IDs not in
    the KB results. v3 adds an explicit "do not invent" rule.
```

Validation: `Prompt(BaseModel)` Pydantic class with `model_config = ConfigDict(extra="forbid")`. CI rejects YAML that does not validate.

### Why a 60-second runtime cache (not zero, not longer)

Each agent invocation calls `langfuse.get_prompt(...)`. Without a cache, that is one HTTPS round-trip on every user message — adds ~50-150ms to first-token latency.

A 60-second cache:
- Eliminates per-message latency overhead
- Means a deploy that changes the production label takes up to 60 seconds to be picked up by all running Cloud Run instances
- Is short enough that a "rollback the bad prompt" emergency is resolved in under a minute

A longer cache (5 minutes) was considered but rejected: the 60-second window matches the user's mental model for a hotfix.

### Why Langfuse over LangSmith Prompts

LangSmith has the same prompt-management feature. Already rejected for observability in ADR-0009 (5,000 traces/month free, 10x less than Langfuse). The same reasoning applies here. Using two prompt-management backends would add a fork-in-the-road for any contributor.

### Why labels (not branches)

Langfuse uses **labels** (production, staging, experiment-a) that point to specific versions. This is the deployment model.

The alternative was branches (a `prod` branch that contains a specific prompt set). Branches make rollback equivalent to "delete the bad commit" — slower than "move the production label back one version."

Labels are more flexible: a single prompt version can be in `production` and `experiment-a` simultaneously, supporting A/B test patterns without duplicating data.

### Failure modes and the local fallback

| Failure | Behavior |
|---|---|
| Langfuse Cloud down (5xx) | Loader retries once with 1-second backoff, then falls back to local YAML; emits Sentry warning |
| Langfuse Cloud slow (>5s) | Loader times out, falls back to local YAML; emits Sentry warning |
| Local YAML missing | `LoaderError` raised; orchestrator returns RFC 7807 error with `type=https://auditpilot.dev/errors/prompt-unavailable` |
| Langfuse production label set to wrong version | Operator moves label back via Langfuse UI; takes effect within 60 seconds |
| YAML schema invalid in PR | CI Pydantic validation blocks merge |
| Mismatch between YAML and Langfuse production version | Daily reconciliation job runs `git diff` against fetched prompts; opens GitHub issue if drift detected |

---

## Consequences

### Positive

- Source of truth is a file in the repo; PR review on prompt changes
- Runtime mutability via Langfuse production label; rollback in under 60 seconds
- Promptfoo evals run against the repo files, guaranteeing what is evaluated is what is reviewed
- Local YAML fallback means a Langfuse outage is degraded, not broken
- Each prompt invocation links the trace to the specific prompt version; eval failures can be tracked to the prompt change
- Forks can swap Langfuse for any other prompt host (or remove the runtime fetch entirely) by changing one file

### Negative

- Two sources of truth (repo + Langfuse) create a daily reconciliation requirement; without the reconciliation job, drift is silent
- The 60-second cache window means a hotfix takes up to 60 seconds to apply across Cloud Run instances
- CI step that pushes prompts to Langfuse needs an API key with prompt-write scope; rotation policy required
- Adds ~150 lines of loader + reconciliation code

---

## Implementation sketch

```python
# apps/api/agents/prompts/loader.py
class PromptLoader:
    def __init__(self, langfuse: Langfuse, local_dir: Path, cache_ttl: int = 60):
        self.langfuse = langfuse
        self.local_dir = local_dir
        self.cache: dict[str, tuple[Prompt, float]] = {}
        self.cache_ttl = cache_ttl

    async def get(self, name: str) -> Prompt:
        cached = self.cache.get(name)
        if cached and time.time() - cached[1] < self.cache_ttl:
            return cached[0]
        try:
            data = await asyncio.wait_for(
                self.langfuse.get_prompt(name, label="production"),
                timeout=5.0,
            )
            prompt = Prompt.model_validate(data)
        except (TimeoutError, LangfuseError) as e:
            sentry_sdk.capture_message(f"Langfuse unreachable, using local: {e}", level="warning")
            prompt = self._load_local(name)
        self.cache[name] = (prompt, time.time())
        return prompt

    def _load_local(self, name: str) -> Prompt:
        path = self.local_dir / f"{name}/production.yaml"
        return Prompt.model_validate(yaml.safe_load(path.read_text()))
```

The CI push:

```yaml
# .github/workflows/deploy-prompts.yml
on:
  push:
    branches: [main]
    paths: ["apps/api/agents/prompts/**"]
jobs:
  push-to-langfuse:
    steps:
      - uses: actions/checkout@v4
      - run: python scripts/push_prompts_to_langfuse.py
        env:
          LANGFUSE_API_KEY: ${{ secrets.LANGFUSE_API_KEY }}
```

---

## Alternatives Considered

| Option | Why rejected |
|---|---|
| **YAML files only (no runtime backend)** | No runtime mutability. Hotfix requires redeploy. No A/B testing. |
| **Langfuse only (no repo-side YAML)** | No PR review. CI evals cannot run without API keys exposed to forks. Lost on Langfuse outage. |
| **LangSmith Prompts** | Already rejected for observability in ADR-0009. Same reasoning. |
| **GitHub repo as the only source of truth, fetched at runtime via raw URL** | Adds GitHub API rate-limit risk to the runtime path. No version labels. No A/B testing. |
| **Embedded f-strings in agent code** | The naive baseline. No versioning, no runtime control, no non-engineer surface. |
| **Custom prompt service** | Builds infrastructure that Langfuse already provides. No portfolio differentiator (unlike the queue ADR, where Redis Streams shows queue mechanics). |

---
