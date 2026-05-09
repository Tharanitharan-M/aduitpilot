# drift-watcher-mcp

MCP server for SOC 2 readiness drift detection. Part of the [AuditPilot](https://github.com/Tharanitharan-M/auditpilot) reference architecture.

## What it does

Provides three MCP tools that let the orchestrator compare evidence snapshots over time, surface drift events, and mark them resolved:

- **diff_snapshots** — compute the diff between two evidence projections, with cosmetic keys (timestamps, ETags, link fields) stripped before hashing
- **list_drift_events** — list persisted drift events for a user, newest first
- **mark_event_resolved** — close out a drift event after the user has fixed the underlying configuration

## Drift semantics

A drift event fires when the SHA-256 hash of the normalized evidence projection changes between scans. The projection strips the following cosmetic keys before hashing (system-design 13.2):

- `fetched_at`, `etag`, `_links`, `node_id`
- `url`, `html_url`, `api_url`, `links`
- `updated_at`, `_meta`

False positive policy (system-design 13.3):

- Cosmetic changes are filtered by the projection
- Status flapping requires two consecutive scans with the same new state before firing
- The first scan never emits drift events (baseline only)

## Installation

```bash
pip install drift-watcher-mcp
```

Or from source:

```bash
cd packages/drift-watcher-mcp
pip install -e ".[dev]"
```

## Usage

### As an MCP server (stdio transport)

```bash
drift-watcher-mcp
```

### As a Python library (in-process)

```python
from drift_watcher_mcp.tools import diff_snapshots, normalize_projection
from drift_watcher_mcp.schemas import DriftSnapshot

prev = DriftSnapshot(
    control_id="CC6.1",
    projection=normalize_projection({"enforcement": "active", "fetched_at": "2026-01-01"}),
)
curr = DriftSnapshot(
    control_id="CC6.1",
    projection=normalize_projection({"enforcement": "disabled", "fetched_at": "2026-05-01"}),
)
result = diff_snapshots(None, None, prev_snapshot=prev, current_snapshot=curr)
assert result.drifted
assert result.events[0].severity == "high"  # `enforcement` is in the high-signal list
```

## Architecture

- **Pydantic v2 schemas** with `extra="forbid"` for strict MCP tool input/output validation
- **Pure-function tools** so the API process imports the same code the MCP server exposes
- **Hash-only short-circuit** — when the caller already has both projection hashes, the tool returns early
- **Configurable fetcher/resolver** for `list_drift_events` and `mark_event_resolved` so the MCP server stays decoupled from any DB driver

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src/ tests/
```

## License

Apache-2.0. See [LICENSE](LICENSE).
