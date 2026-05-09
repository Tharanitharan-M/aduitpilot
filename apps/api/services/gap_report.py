"""Markdown gap report assembler (Sprint 8 chunk 8.7).

Renders a concise, human-readable Markdown document from one
mock-readiness-challenge run. Sections:

  1. Run summary (timestamp, budget, A2A task state).
  2. Failing-control snapshot pulled from the control_map projection.
  3. Adversarial objections, severity-sorted (critical first).
  4. Recommended next steps (one bullet per finding).

The document is uploaded to object storage by the worker and surfaced to
the user behind a 15-minute pre-signed URL on the dashboard.

The renderer never embeds the four AICPA UPAct surface forms without a
safe prefix so the language guard does not flag the rendered output.

Refs: PLAN.md chunk 8.7; ADR-0002.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SEVERITY_BADGE = {
    "critical": "**CRITICAL**",
    "high": "**HIGH**",
    "medium": "_medium_",
    "low": "_low_",
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class GapReportContext(BaseModel):
    """Inputs to :func:`render_gap_report`.

    Pydantic v2 model so the renderer cannot be invoked with malformed
    fields. Findings remain ``list[dict[str, Any]]`` because the worker
    upstream of this assembler is the canonical schema enforcer
    (``MockAuditRunHandler`` validates each finding's severity before
    persisting). The renderer is defensive — it tolerates surprising
    severity strings rather than rejecting the report wholesale.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    user_id: str
    summary: str
    findings: list[dict[str, Any]]
    scan_context: dict[str, Any]
    budget: dict[str, float | int] = Field(default_factory=dict)
    state: str = "TASK_STATE_COMPLETED"
    error: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def render_gap_report(ctx: GapReportContext) -> str:
    sections: list[str] = []
    sections.append(_header(ctx))
    sections.append(_run_summary(ctx))
    sections.append(_failing_controls_snapshot(ctx.scan_context))
    sections.append(_findings_section(ctx.findings))
    sections.append(_next_steps_section(ctx.findings))
    sections.append(_footer(ctx))
    return "\n\n".join(s for s in sections if s).strip() + "\n"


def _header(ctx: GapReportContext) -> str:
    return (
        "# Mock readiness challenge — draft gap report\n\n"
        "_Internal adversarial pass over a draft readiness assessment. "
        "This is a draft document for human review. AuditPilot is a "
        "readiness reference architecture and does not produce a "
        "CPA-signed report._"
    )


def _run_summary(ctx: GapReportContext) -> str:
    spent = float(ctx.budget.get("spent_usd", 0.0)) if ctx.budget else 0.0
    cap = float(ctx.budget.get("cap_usd", 0.0)) if ctx.budget else 0.0
    calls = int(ctx.budget.get("calls", 0)) if ctx.budget else 0
    state_label = {
        "TASK_STATE_COMPLETED": "Completed",
        "TASK_STATE_BUDGET_EXCEEDED": "Halted on budget cap",
        "TASK_STATE_FAILED": "Failed",
    }.get(ctx.state, ctx.state)
    lines = [
        "## Run summary",
        f"- **Run id:** `{ctx.run_id}`",
        f"- **Generated at (UTC):** {ctx.generated_at.isoformat(timespec='seconds')}",
        f"- **A2A task state:** {state_label}",
        f"- **Findings:** {len(ctx.findings)}",
        f"- **Budget:** ${spent:.4f} spent of ${cap:.4f} cap ({calls} call(s))",
    ]
    if ctx.error:
        lines.append(f"- **Error:** {ctx.error}")
    if ctx.summary:
        lines.extend(["", "**Adversarial summary**", "", _quote(ctx.summary)])
    return "\n".join(lines)


def _failing_controls_snapshot(scan_context: dict[str, Any]) -> str:
    control_map = scan_context.get("control_map") or []
    failing: list[dict[str, Any]] = []
    if isinstance(control_map, list):
        for row in control_map:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status", "")).lower()
            if status in {"failing", "partial"}:
                failing.append(row)
    elif isinstance(control_map, dict):
        for key, value in control_map.items():
            if not isinstance(value, dict):
                continue
            status = str(value.get("status", "")).lower()
            if status in {"failing", "partial"}:
                failing.append({"tsc_id": key, **value})

    lines = ["## FAILING / partial controls (orchestrator snapshot)"]
    if not failing:
        lines.append("_No failing or partial controls were present in the orchestrator snapshot._")
        return "\n".join(lines)

    lines.append("| TSC clause / control | Status | Confidence | Rationale |")
    lines.append("| --- | --- | --- | --- |")
    for row in failing[:30]:
        ident = row.get("tsc_id") or row.get("control_id") or ""
        status = row.get("status", "")
        confidence = row.get("confidence", "")
        confidence_str = (
            f"{confidence:.2f}" if isinstance(confidence, float) else str(confidence)
        )
        rationale = str(row.get("rationale") or "").replace("|", "\\|").replace("\n", " ")
        if len(rationale) > 240:
            rationale = rationale[:237] + "…"
        lines.append(f"| `{ident}` | {status} | {confidence_str} | {rationale} |")
    if len(failing) > 30:
        lines.append(f"\n_… {len(failing) - 30} more failing/partial rows truncated._")
    return "\n".join(lines)


def _findings_section(findings: list[dict[str, Any]]) -> str:
    lines = ["## Adversarial objections (severity-sorted)"]
    if not findings:
        lines.append(
            "_AdversarialAuditor returned no objections — the draft assessment is "
            "honestly defensible at the scope provided._"
        )
        return "\n".join(lines)

    sorted_findings = sorted(
        findings,
        key=lambda f: SEVERITY_ORDER.get(str(f.get("severity", "low")), 99),
    )
    for idx, finding in enumerate(sorted_findings, 1):
        severity = str(finding.get("severity", "low")).lower()
        badge = SEVERITY_BADGE.get(severity, severity)
        tsc = finding.get("tsc_id") or "—"
        objection = str(finding.get("objection", "")).strip() or "_(no objection text)_"
        lines.append("")
        lines.append(f"### {idx}. {badge} — `{tsc}`")
        lines.append("")
        lines.append(objection)
    return "\n".join(lines)


def _next_steps_section(findings: list[dict[str, Any]]) -> str:
    actionable = [
        f for f in findings if str(f.get("recommended_next_step", "")).strip()
    ]
    if not actionable:
        return ""
    lines = ["## Recommended next steps"]
    sorted_findings = sorted(
        actionable,
        key=lambda f: SEVERITY_ORDER.get(str(f.get("severity", "low")), 99),
    )
    for finding in sorted_findings:
        severity = str(finding.get("severity", "low")).lower()
        tsc = finding.get("tsc_id") or "general"
        step = str(finding.get("recommended_next_step", "")).strip()
        lines.append(f"- [{severity}] `{tsc}` — {step}")
    return "\n".join(lines)


def _footer(ctx: GapReportContext) -> str:  # noqa: ARG001
    return (
        "---\n\n"
        "_Generated by AuditPilot AdversarialAuditor (reference architecture). "
        "All recommendations require human review before any change is made._"
    )


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


__all__ = ["GapReportContext", "render_gap_report"]
