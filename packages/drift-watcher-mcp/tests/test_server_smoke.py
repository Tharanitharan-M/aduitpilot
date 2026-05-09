"""Server-import smoke test (mcp-server-validator F1).

Catches the failure mode where a FastMCP API change (e.g. v3 dropping
the ``description=`` kwarg) silently breaks the entrypoint without
breaking any tool/schema test.
"""

from __future__ import annotations


def test_server_module_imports():
    from drift_watcher_mcp.server import main, mcp

    assert mcp is not None
    assert callable(main)


def test_server_has_three_tools():
    """The MCP server exposes the contracted three tools.

    We import server.py to populate the FastMCP tool registry, then
    inspect via the imported tool callables (not via the registry,
    which is a private API surface).
    """
    from drift_watcher_mcp import server

    for name in ("diff_snapshots", "list_drift_events", "mark_event_resolved"):
        assert hasattr(server, name), f"Missing tool: {name}"
