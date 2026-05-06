"""Sprint 5 chunk 5.23 — SSE bridge data-chunk regression tests.

The SSE bridge emits two new typed-data chunks for the dashboard's grid
and evidence cards:

  - ``data-control-map``  — list of ControlAssessment dicts
  - ``data-evidence-rows`` — list of Evidence dicts

These tests cover the helper extractors in isolation so a future
refactor of ``ui_message_stream_from_graph_updates`` cannot silently
break the dashboard's live update path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from apps.api.sse.ai_sdk_v6 import (
    DataControlMapChunk,
    DataEvidenceRowsChunk,
    _extract_control_map,
    _extract_evidence,
    sse_encode,
)
from apps.api.state import ControlAssessment, Evidence

# ── _extract_control_map ─────────────────────────────────────────────────────


def test_extract_control_map_none_for_empty_update() -> None:
    assert _extract_control_map(None) is None
    assert _extract_control_map({}) is None
    assert _extract_control_map({"messages": []}) is None
    assert _extract_control_map({"control_map": {}}) is None


def test_extract_control_map_serialises_pydantic_models() -> None:
    update = {
        "control_map": {
            "CC6.1": ControlAssessment(
                tsc_id="CC6.1",
                status="failing",
                confidence=0.6,
                nist_800_53_refs=["AC-1", "AC-2"],
                evidence_ids=["ev-1"],
                rationale="Branch protection disabled.",
            ),
        }
    }
    rows = _extract_control_map(update)
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["tsc_id"] == "CC6.1"
    assert rows[0]["status"] == "failing"
    assert rows[0]["nist_800_53_refs"] == ["AC-1", "AC-2"]


def test_extract_control_map_seeds_tsc_id_from_dict_key() -> None:
    """If the inner dict ever loses its tsc_id field, the dict key fills in."""
    update = {
        "control_map": {
            "CC7.1": {
                "status": "passing",
                "confidence": 1.0,
                "nist_800_53_refs": [],
                "evidence_ids": [],
                "rationale": None,
            }
        }
    }
    rows = _extract_control_map(update)
    assert rows is not None
    assert rows[0]["tsc_id"] == "CC7.1"


# ── _extract_evidence ────────────────────────────────────────────────────────


def test_extract_evidence_none_for_empty_update() -> None:
    assert _extract_evidence(None) is None
    assert _extract_evidence({}) is None
    assert _extract_evidence({"evidence": []}) is None


def test_extract_evidence_serialises_pydantic_models() -> None:
    ev = Evidence(
        id="ev-001",
        source_type="github",
        source_uri="github://owner/repo/branch-protection",
        raw={"check_type": "branch-protection", "status": "passing"},
        content_hash="a" * 64,
        collected_at=datetime(2026, 5, 6, tzinfo=UTC),
        scan_run_id="sr-1",
    )
    rows = _extract_evidence({"evidence": [ev]})
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["id"] == "ev-001"
    assert rows[0]["source_type"] == "github"
    assert rows[0]["raw"]["check_type"] == "branch-protection"


# ── sse_encode round-trip ────────────────────────────────────────────────────


def test_data_control_map_chunk_round_trip() -> None:
    chunk = DataControlMapChunk(
        id="cm_test",
        data=[{"tsc_id": "CC6.1", "status": "failing", "confidence": 0.5}],
    )
    line = sse_encode(chunk)
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    payload = json.loads(line[len("data: ") : -2])
    assert payload["type"] == "data-control-map"
    assert payload["id"] == "cm_test"
    assert payload["data"][0]["tsc_id"] == "CC6.1"


def test_data_evidence_rows_chunk_round_trip() -> None:
    chunk = DataEvidenceRowsChunk(
        id="ev_test",
        data=[{"id": "ev-1", "source_type": "github"}],
    )
    line = sse_encode(chunk)
    payload = json.loads(line[len("data: ") : -2])
    assert payload["type"] == "data-evidence-rows"
    assert payload["data"][0]["source_type"] == "github"
