"""Tool tests for drift-watcher-mcp (Sprint 9 chunks 9.2, 9.3).

Covers:
  - normalize_projection strips cosmetic keys + sorts + recurses
  - projection_hash is stable for re-orderings
  - diff_snapshots short-circuits when prev_hash == current_hash
  - diff_snapshots emits config_changed events with the right severity
  - diff_snapshots emits evidence_removed when current is empty
  - list_drift_events / mark_event_resolved respect the injected callbacks
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from drift_watcher_mcp.schemas import DriftEventOut, DriftSnapshot
from drift_watcher_mcp.tools import (
    configure,
    diff_snapshots,
    list_drift_events,
    mark_event_resolved,
    normalize_projection,
    projection_hash,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


class TestNormalize:
    def test_strips_cosmetic_keys(self):
        raw = {
            "enforcement": "active",
            "fetched_at": "2026-01-01",
            "etag": "abc",
            "node_id": "nd_1",
            "_links": {"self": "x"},
        }
        out = normalize_projection(raw)
        assert "fetched_at" not in out
        assert "etag" not in out
        assert "_links" not in out
        assert "node_id" not in out
        assert out["enforcement"] == "active"

    def test_sorts_keys(self):
        a = normalize_projection({"b": 1, "a": 2, "c": 3})
        assert list(a.keys()) == ["a", "b", "c"]

    def test_recurses_into_dicts(self):
        out = normalize_projection({
            "rules": {"fetched_at": "x", "require_pr": True},
        })
        assert out["rules"] == {"require_pr": True}

    def test_recurses_into_list_of_dicts(self):
        out = normalize_projection({
            "branches": [
                {"name": "main", "etag": "x"},
                {"name": "develop", "fetched_at": "y"},
            ]
        })
        assert out["branches"] == [
            {"name": "main"},
            {"name": "develop"},
        ]

    def test_empty_returns_empty(self):
        assert normalize_projection({}) == {}
        assert normalize_projection(None) == {}


class TestProjectionHash:
    def test_stable_across_key_order(self):
        a = projection_hash({"a": 1, "b": 2})
        b = projection_hash({"b": 2, "a": 1})
        assert a == b

    def test_changes_on_value_change(self):
        a = projection_hash({"a": 1})
        b = projection_hash({"a": 2})
        assert a != b

    def test_empty_returns_empty_string(self):
        assert projection_hash({}) == ""
        assert projection_hash(None) == ""


class TestDiffSnapshotsShortCircuit:
    def test_equal_hashes_returns_no_drift(self):
        r = diff_snapshots("abc", "abc")
        assert r.drifted is False
        assert r.events == []

    def test_unequal_hashes_no_bodies_returns_drift_no_events(self):
        r = diff_snapshots("abc", "def")
        assert r.drifted is True
        assert r.events == []


class TestDiffSnapshotsWithBodies:
    def test_config_changed_medium(self):
        prev = DriftSnapshot(
            control_id="CC6.1",
            projection=normalize_projection({"foo": "a"}),
        )
        curr = DriftSnapshot(
            control_id="CC6.1",
            projection=normalize_projection({"foo": "b"}),
        )
        r = diff_snapshots(None, None, prev_snapshot=prev, current_snapshot=curr)
        assert r.drifted is True
        assert len(r.events) == 1
        assert r.events[0].event_type == "config_changed"
        assert r.events[0].severity == "medium"
        assert "changed" in r.events[0].what_changed

    def test_high_signal_key_removal_is_high_severity(self):
        prev = DriftSnapshot(
            control_id="CC6.1",
            projection=normalize_projection({
                "enforcement": "active",
                "required_pull_request_reviews": True,
            }),
        )
        curr = DriftSnapshot(
            control_id="CC6.1",
            projection=normalize_projection({"required_pull_request_reviews": True}),
        )
        r = diff_snapshots(None, None, prev_snapshot=prev, current_snapshot=curr)
        assert r.drifted is True
        assert r.events[0].severity == "high"
        assert "enforcement" in r.events[0].what_changed

    def test_evidence_removed(self):
        prev = DriftSnapshot(
            control_id="CC6.1",
            projection=normalize_projection({"foo": "a"}),
        )
        curr = DriftSnapshot(control_id="CC6.1", projection={})
        r = diff_snapshots(None, None, prev_snapshot=prev, current_snapshot=curr)
        assert r.drifted is True
        assert r.events[0].event_type == "evidence_removed"
        assert r.events[0].severity == "high"

    def test_first_snapshot_returns_baseline_event(self):
        prev = DriftSnapshot(control_id="CC6.1", projection={})
        curr = DriftSnapshot(
            control_id="CC6.1",
            projection=normalize_projection({"foo": "a"}),
        )
        r = diff_snapshots(None, None, prev_snapshot=prev, current_snapshot=curr)
        # Drifted from empty -> non-empty: still emits the event but
        # the drift detector treats this as baseline.
        assert r.drifted is True
        assert r.events[0].severity == "low"

    def test_identical_projections_returns_no_drift(self):
        proj = normalize_projection({"foo": "a"})
        prev = DriftSnapshot(control_id="CC6.1", projection=proj)
        curr = DriftSnapshot(control_id="CC6.1", projection=proj)
        r = diff_snapshots(None, None, prev_snapshot=prev, current_snapshot=curr)
        assert r.drifted is False


class TestListDriftEvents:
    def test_no_fetcher_returns_empty(self):
        r = list_drift_events("user_x")
        assert r.count == 0
        assert r.events == []

    def test_uses_configured_fetcher(self):
        sample = DriftEventOut(
            id="11111111-1111-1111-1111-111111111111",
            user_id="user_x",
            control_id="CC6.1",
            event_type="config_changed",
            detected_at=datetime(2026, 5, 9, tzinfo=UTC),
        )
        configure(fetcher=lambda user_id, since: [sample] if user_id == "user_x" else [])
        r = list_drift_events("user_x")
        assert r.count == 1
        assert r.events[0].id == sample.id

    def test_fetcher_dict_validates_into_model(self):
        configure(
            fetcher=lambda user_id, since: [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "user_id": user_id,
                    "control_id": "CC6.1",
                    "event_type": "config_changed",
                    "detected_at": "2026-05-09T00:00:00Z",
                }
            ]
        )
        r = list_drift_events("user_x")
        assert r.count == 1
        assert r.events[0].user_id == "user_x"


class TestMarkEventResolved:
    def test_no_resolver_returns_not_ok(self):
        r = mark_event_resolved("user_x", "evt_1")
        assert r.ok is False

    def test_uses_configured_resolver(self):
        configure(resolver=lambda user_id, event_id: True)
        r = mark_event_resolved("user_x", "evt_1")
        assert r.ok is True
        assert r.new_status == "resolved"

    def test_resolver_returning_false(self):
        configure(resolver=lambda user_id, event_id: False)
        r = mark_event_resolved("user_x", "evt_1")
        assert r.ok is False
