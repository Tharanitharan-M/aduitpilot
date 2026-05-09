# Changelog

All notable changes to `drift-watcher-mcp` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-05-09

### Added
- `diff_snapshots` tool: SHA-256-keyed normalised projection diff with cosmetic-key stripping (system-design 13.2)
- `list_drift_events` tool: lists persisted drift events for a user, configurable fetcher
- `mark_event_resolved` tool: closes a drift event, configurable resolver
- Pydantic v2 schemas with `extra="forbid"` on every model
- Hash-only short-circuit when both projection hashes are supplied
- High-signal-key heuristic to upgrade severity on enforcement / branch-protection key changes
- 30+ pytest cases covering normalisation, hashing, classification, and configurable callbacks
- stdio transport via `fastmcp.FastMCP`
- Apache-2.0 license

### Notes
- `status_changed` events are emitted by the API-side detector at `apps/api/services/drift.py`,
  not by this MCP server, because they require the readiness state machine view of evidence.
- 2-scan flap protection (system-design 13.3) lives in the API-side detector for the same reason.
