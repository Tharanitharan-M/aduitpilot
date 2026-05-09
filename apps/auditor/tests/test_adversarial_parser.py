"""Sprint 8 chunk 8.2 — output parsing of the AdversarialAuditor agent."""

from __future__ import annotations

from apps.auditor.agents.adversarial import (
    AdversarialFinding,
    AdversarialResult,
    parse_findings_payload,
)


def test_parses_strict_json() -> None:
    raw = """
    {"summary": "Test summary", "findings": [
        {"severity": "high", "tsc_id": "CC6.6",
         "objection": "Branch protection only on main",
         "recommended_next_step": "Apply protection to release branches"},
        {"severity": "low", "tsc_id": null,
         "objection": "Confidence inflated by single-rep evidence",
         "recommended_next_step": "Re-scan with all repos"}
    ]}
    """
    result = parse_findings_payload(raw)
    assert isinstance(result, AdversarialResult)
    assert result.status == "completed"
    assert len(result.findings) == 2
    assert result.findings[0].severity == "high"
    assert result.findings[1].tsc_id is None


def test_parses_fenced_json() -> None:
    raw = """```json
    {"summary": "ok", "findings": []}
    ```"""
    result = parse_findings_payload(raw)
    assert result.status == "completed"
    assert result.summary == "ok"
    assert result.findings == []


def test_invalid_json_returns_failed() -> None:
    result = parse_findings_payload("not json at all")
    assert result.status == "failed"
    assert result.error is not None


def test_drops_malformed_finding_but_keeps_rest() -> None:
    raw = """
    {"summary": "x", "findings": [
        {"severity": "high", "objection": "ok"},
        {"severity": "not-a-real-severity", "objection": "x"},
        "string-instead-of-object"
    ]}
    """
    result = parse_findings_payload(raw)
    assert result.status == "completed"
    assert len(result.findings) == 1
    assert isinstance(result.findings[0], AdversarialFinding)
