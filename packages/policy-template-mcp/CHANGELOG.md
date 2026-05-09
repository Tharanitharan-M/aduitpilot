# Changelog

All notable changes to `policy-template-mcp` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-05-08

### Added
- `list_templates` tool: enumerate the four built-in policy templates (IRP, Access Control, Change Management, Vendor Management)
- `get_template` tool: fetch one template by id with metadata
- `render_template` tool: Jinja2 render with strict variable schema and TSC clause citations
- Four built-in templates with `[CC*]` citation markers:
  - `incident_response_plan` (IRP) — full policy body with adversarial review checklist
  - `access_control_policy` — covers CC6.1, CC6.2, CC6.3, CC6.6
  - `change_management_policy` — covers CC8.1
  - `vendor_management_policy` — covers CC9.1, CC9.2
- Pydantic v2 schemas with `extra="forbid"` on every model
- JSON Schema export from Pydantic models used as MCP tool `inputSchema`
- stdio transport via `fastmcp.FastMCP`
- 21 pytest cases covering listing, fetching, and rendering
- Apache-2.0 license

### Notes
- Templates output draft policies for the human reviewer to edit at the HumanReviewGate
  (LangGraph `interrupt()` per ADR-0007). Never overwrites a finalized version.
