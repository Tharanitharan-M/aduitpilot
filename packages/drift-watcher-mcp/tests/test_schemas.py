"""Schema tests for drift-watcher-mcp (Sprint 9 chunks 9.1-9.3).

Mirrors policy-template-mcp's test_schemas pattern: every Pydantic v2
model must reject extra keys (``extra="forbid"``) and survive a
JSON Schema export so the MCP tool surface stays validatable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from drift_watcher_mcp.schemas import (
    COSMETIC_KEYS,
    DiffEvent,
    DiffResult,
    DriftEventOut,
    DriftSnapshot,
    ListDriftEventsResult,
    MarkResolvedResult,
)


class TestExtraForbid:
    @pytest.mark.parametrize(
        "model",
        [DriftSnapshot, DiffEvent, DiffResult, DriftEventOut, MarkResolvedResult],
    )
    def test_rejects_extra_keys(self, model):
        with pytest.raises(ValidationError):
            model.model_validate({"control_id": "CC6.1", "id": "x", "user_id": "u",
                                  "event_type": "config_changed",
                                  "detected_at": "2026-05-09T00:00:00Z",
                                  "what_changed": "x", "extra_key": "no"})


class TestDriftSnapshot:
    def test_minimal(self):
        s = DriftSnapshot(control_id="CC6.1")
        assert s.control_id == "CC6.1"
        assert s.projection == {}
        assert s.projection_hash == ""

    def test_full(self):
        s = DriftSnapshot(
            control_id="CC6.1",
            projection={"enforcement": "active"},
            projection_hash="abc123",
            source_link="https://github.com/example/repo/settings/branches",
            captured_at=datetime(2026, 5, 9, tzinfo=UTC),
        )
        assert s.projection_hash == "abc123"
        assert s.captured_at.tzinfo is UTC


class TestDiffResult:
    def test_no_drift(self):
        r = DiffResult(prev_hash="abc", current_hash="abc")
        assert r.drifted is False
        assert r.events == []

    def test_with_events(self):
        ev = DiffEvent(
            control_id="CC6.1",
            event_type="config_changed",
            what_changed="x",
            previous_value={"a": 1},
            current_value={"a": 2},
            severity="medium",
        )
        r = DiffResult(prev_hash="a", current_hash="b", drifted=True, events=[ev])
        assert r.drifted is True
        assert r.events[0].severity == "medium"


class TestDriftEventOut:
    def test_round_trip(self):
        e = DriftEventOut(
            id="11111111-1111-1111-1111-111111111111",
            user_id="user_x",
            control_id="CC6.1",
            event_type="status_changed",
            what_changed="MFA disabled",
            previous_value={"mfa": True},
            current_value={"mfa": False},
            suggested_fix="Re-enable org MFA",
            source_link="https://github.com/orgs/example/settings/security",
            severity="high",
            detected_at=datetime(2026, 5, 9, tzinfo=UTC),
            status="open",
            content_hash="cafebabe",
        )
        assert e.event_type == "status_changed"
        assert e.severity == "high"
        # JSON round-trip survives the typing
        as_json = e.model_dump_json()
        again = DriftEventOut.model_validate_json(as_json)
        assert again == e


class TestMarkResolvedResult:
    def test_default_status(self):
        r = MarkResolvedResult(event_id="x")
        assert r.ok is True
        assert r.new_status == "resolved"


class TestListDriftEventsResult:
    def test_empty(self):
        r = ListDriftEventsResult()
        assert r.count == 0
        assert r.events == []


class TestCosmeticKeys:
    def test_includes_known_keys(self):
        for k in ("fetched_at", "etag", "_links", "node_id"):
            assert k in COSMETIC_KEYS
