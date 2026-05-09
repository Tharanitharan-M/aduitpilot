"""Sprint 9 chunk 9.4 — drift detector + flap protection tests.

Covers:
  - First-ever sighting writes baseline + emits no event.
  - Pending hash is parked on first sighting; no event emitted.
  - Same pending_hash on the SECOND scan promotes pending -> confirmed
    AND emits a drift_events row.
  - A repeat scan with the same confirmed_hash is a no-op (clears
    pending).
  - Re-fire suppression: same content_hash -> no new row.
  - Opt-out via monitored_controls suppresses the event.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from apps.api.services.drift import ControlSnapshot, detect_drift

# ── In-memory pool stand-in ─────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, store: dict) -> None:
        self._store = store
        self._rows: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query: str, params: tuple = ()) -> None:
        q = " ".join(query.split())
        if q.startswith("SET") or q.startswith("SELECT set_config"):
            self._rows = []
            return

        if q.startswith("SELECT control_id FROM monitored_controls"):
            user_id = params[0]
            self._rows = [
                (cid,)
                for (uid, cid), monitored in self._store["monitored"].items()
                if uid == user_id and monitored is False
            ]
            return

        if q.startswith("SELECT control_id, confirmed_hash, confirmed_value, pending_hash"):
            user_id, control_ids = params
            wanted = set(control_ids)
            self._rows = []
            for (uid, cid), data in self._store["snapshots"].items():
                if uid == user_id and cid in wanted:
                    self._rows.append(
                        (
                            cid,
                            data.get("confirmed_hash", ""),
                            data.get("confirmed_value", {}),
                            data.get("pending_hash", ""),
                            data.get("pending_value", {}),
                            data.get("source_link"),
                            data.get("pending_seen_at"),
                        )
                    )
            return

        if q.startswith("INSERT INTO drift_snapshots"):
            (
                user_id,
                control_id,
                confirmed_hash,
                confirmed_value_json,
                pending_hash,
                pending_value_json,
                _pending_check,
                source_link,
            ) = params
            self._store["snapshots"][(user_id, control_id)] = {
                "confirmed_hash": confirmed_hash,
                "confirmed_value": json.loads(confirmed_value_json),
                "pending_hash": pending_hash,
                "pending_value": json.loads(pending_value_json),
                "source_link": source_link,
            }
            return

        if q.startswith("SELECT id::text FROM drift_events WHERE user_id = %s AND content_hash"):
            user_id, content_hash = params
            for ev in self._store["events"]:
                if (
                    ev["user_id"] == user_id
                    and ev["content_hash"] == content_hash
                    and ev["status"] in {"open", "dismissed"}
                ):
                    self._rows = [(ev["id"],)]
                    return
            self._rows = []
            return

        if q.startswith("INSERT INTO drift_events"):
            (
                event_id,
                user_id,
                control_id,
                event_type,
                what_changed,
                previous_value_json,
                current_value_json,
                suggested_fix,
                source_link,
                severity,
                content_hash,
            ) = params
            self._store["events"].append(
                {
                    "id": event_id,
                    "user_id": user_id,
                    "control_id": control_id,
                    "event_type": event_type,
                    "what_changed": what_changed,
                    "previous_value": json.loads(previous_value_json),
                    "current_value": json.loads(current_value_json),
                    "suggested_fix": suggested_fix,
                    "source_link": source_link,
                    "severity": severity,
                    "content_hash": content_hash,
                    "status": "open",
                }
            )
            return

        raise AssertionError(f"unexpected SQL: {q!r}")

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, store: dict) -> None:
        self._store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _FakeCursor(self._store)

    def transaction(self):
        return self

    async def execute(self, query: str, params: tuple = ()) -> Any:
        cur = _FakeCursor(self._store)
        await cur.execute(query, params)
        return cur


class _FakePool:
    def __init__(self) -> None:
        self.store: dict = {
            "monitored": {},
            "snapshots": {},
            "events": [],
        }

    def connection(self):
        return _FakeConn(self.store)


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_baseline_first_scan_emits_no_event():
    pool = _FakePool()
    snap = ControlSnapshot(
        control_id="CC6.1",
        projection={"enforcement": "active"},
    )
    outcome = await detect_drift(pool, user_id="user_1", snapshots=[snap])
    assert outcome.snapshots_seen == 1
    assert outcome.events_emitted == 0
    assert pool.store["snapshots"][("user_1", "CC6.1")]["confirmed_hash"] != ""


@pytest.mark.asyncio
async def test_first_sighting_of_change_parks_pending_no_event():
    pool = _FakePool()
    pool.store["snapshots"][("user_1", "CC6.1")] = {
        "confirmed_hash": "OLD",
        "confirmed_value": {"enforcement": "active"},
        "pending_hash": "",
        "pending_value": {},
    }
    snap = ControlSnapshot(
        control_id="CC6.1",
        projection={"enforcement": "disabled"},
    )
    outcome = await detect_drift(pool, user_id="user_1", snapshots=[snap])
    assert outcome.events_emitted == 0
    snap_row = pool.store["snapshots"][("user_1", "CC6.1")]
    # Confirmed hash should still be the old value.
    assert snap_row["confirmed_hash"] == "OLD"
    assert snap_row["pending_hash"] != ""


@pytest.mark.asyncio
async def test_second_consecutive_pending_emits_event():
    pool = _FakePool()
    # Compute the hash the detector will produce for the new projection
    # so we can pre-seed pending_hash to match.
    from drift_watcher_mcp.tools import normalize_projection, projection_hash

    new_proj = normalize_projection({"enforcement": "disabled"})
    new_hash = projection_hash(new_proj)
    pool.store["snapshots"][("user_1", "CC6.1")] = {
        "confirmed_hash": "OLD",
        "confirmed_value": {"enforcement": "active"},
        "pending_hash": new_hash,
        "pending_value": new_proj,
    }
    snap = ControlSnapshot(
        control_id="CC6.1",
        projection={"enforcement": "disabled"},
    )
    outcome = await detect_drift(pool, user_id="user_1", snapshots=[snap])
    assert outcome.events_emitted == 1
    assert pool.store["events"][0]["control_id"] == "CC6.1"
    assert pool.store["events"][0]["severity"] == "high"
    # confirmed_hash advanced to the new state.
    assert pool.store["snapshots"][("user_1", "CC6.1")]["confirmed_hash"] == new_hash


@pytest.mark.asyncio
async def test_no_change_clears_pending():
    pool = _FakePool()
    from drift_watcher_mcp.tools import normalize_projection, projection_hash

    proj = normalize_projection({"enforcement": "active"})
    h = projection_hash(proj)
    pool.store["snapshots"][("user_1", "CC6.1")] = {
        "confirmed_hash": h,
        "confirmed_value": proj,
        "pending_hash": "STALE_PENDING",
        "pending_value": {"enforcement": "x"},
    }
    snap = ControlSnapshot(control_id="CC6.1", projection={"enforcement": "active"})
    outcome = await detect_drift(pool, user_id="user_1", snapshots=[snap])
    assert outcome.events_emitted == 0
    assert pool.store["snapshots"][("user_1", "CC6.1")]["pending_hash"] == ""


@pytest.mark.asyncio
async def test_refire_suppression_with_existing_dismissed_event():
    pool = _FakePool()
    from drift_watcher_mcp.tools import normalize_projection, projection_hash

    from apps.api.services.drift import compute_event_content_hash

    new_proj = normalize_projection({"enforcement": "disabled"})
    new_hash = projection_hash(new_proj)
    content_hash = compute_event_content_hash(
        control_id="CC6.1",
        event_type="config_changed",
        current_value=new_proj,
    )
    # Existing dismissed event with the same content_hash.
    pool.store["events"].append(
        {
            "id": "existing-event-id",
            "user_id": "user_1",
            "control_id": "CC6.1",
            "event_type": "config_changed",
            "what_changed": "...",
            "previous_value": {},
            "current_value": new_proj,
            "suggested_fix": "",
            "source_link": None,
            "severity": "high",
            "content_hash": content_hash,
            "status": "dismissed",
        }
    )
    pool.store["snapshots"][("user_1", "CC6.1")] = {
        "confirmed_hash": "OLD",
        "confirmed_value": {"enforcement": "active"},
        "pending_hash": new_hash,
        "pending_value": new_proj,
    }
    snap = ControlSnapshot(
        control_id="CC6.1",
        projection={"enforcement": "disabled"},
    )
    outcome = await detect_drift(pool, user_id="user_1", snapshots=[snap])
    # No new event row; suppression counter incremented.
    assert outcome.events_emitted == 0
    assert outcome.events_suppressed_by_content_hash == 1
    assert len(pool.store["events"]) == 1


@pytest.mark.asyncio
async def test_optout_skips_control():
    pool = _FakePool()
    pool.store["monitored"][("user_1", "CC6.1")] = False
    snap = ControlSnapshot(
        control_id="CC6.1",
        projection={"enforcement": "disabled"},
    )
    outcome = await detect_drift(pool, user_id="user_1", snapshots=[snap])
    assert outcome.events_suppressed_by_optout == 1
    # Snapshot row should NOT have been touched.
    assert ("user_1", "CC6.1") not in pool.store["snapshots"]
