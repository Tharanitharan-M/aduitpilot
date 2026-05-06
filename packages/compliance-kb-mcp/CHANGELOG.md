# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-05

Published 2026-05-05:
- PyPI: https://pypi.org/project/compliance-kb-mcp/
- npm: https://www.npmjs.com/package/@auditpilot/compliance-kb-mcp

> Artifacts were built 2026-05-02 and published after Sprint 4 end-to-end
> validation confirmed the orchestrator consumes the server correctly over
> the stdio MCP transport.

### Changed

- **Breaking:** Replaced the abridged AICPA-derived SOC 2 TSC dataset with the
  canonical NIST SP 800-53 Revision 5 catalog (324 base controls across 20
  families) sourced from the public-domain OSCAL JSON published at
  [usnistgov/oscal-content](https://github.com/usnistgov/oscal-content).
- Pivoted positioning: AuditPilot maps environments to NIST 800-53 Rev 5 and
  surfaces which SOC 2 Trust Services Criteria are satisfied by that 800-53
  coverage. SOC 2 TSC text remains a copyright-protected AICPA publication and
  is not redistributed; we redistribute SOC 2 TSC identifiers only.
- Renamed schemas: `Control.id` is now a NIST 800-53 base identifier
  (e.g. `AC-1`, `SC-7`, `IA-2`); `Control.framework` is `nist_800_53_rev5`;
  `soc2_tsc_mappings: list[str]` is the new field that stores TSC IDs satisfied.
- Tools updated: `lookup_control(control_id)`, `search_controls(query, k)`,
  and `list_controls(family_id?)` no longer take a `framework` argument.
- New tool: `lookup_by_soc2_tsc(tsc_id)` returns all 800-53 controls mapped to
  a given Trust Services Criteria identifier (e.g. `CC6.1`, `A1.2`).

### Added

- Public-domain dataset rebuilt from `NIST_SP-800-53_rev5_catalog.json` with
  parameter substitution into canonical control statements.
- Curated bidirectional NIST 800-53 ↔ SOC 2 TSC mapping covering all
  Common Criteria, Availability, Confidentiality, Processing Integrity, and
  Privacy clauses.
- `scripts/build_dataset.py` regenerates the dataset from the official OSCAL
  source. The script is idempotent and documents both the publication and
  mapping citations on every control.
- New unit tests in `tests/test_schemas.py` and `tests/test_tools.py` covering
  family coverage, citation completeness, NIST and TSC pattern validation, and
  the new lookup paths.

### Fixed

- `scripts/build_dataset.py` `_substitute_params` now iterates to a fixed point
  (capped at 8 passes). The previous single-pass implementation left nested
  `{{ insert: param, ... }}` markers inside `select.choice` strings
  unsubstituted in 23 controls (AC-7, AC-11, AC-20, CA-3, CM-3, PE-3, PE-14,
  PL-4, RA-3, RA-6, SA-4, SA-22, SC-6, SC-42, SI-3, SI-4, SI-5, SI-6, SI-14,
  SR-3, SR-8, SR-10, SR-11). Detected by a new regression test
  (`test_no_unsubstituted_oscal_param_placeholders`).

### Removed

- `data/soc2_tsc_controls.json` (the abridged Probo-derived dataset).
- The legacy `framework="soc2"` literal and the `Control.points_of_focus`
  field that previously stored TODO pointers to AICPA copyrighted text.

## [0.1.0] - 2026-05-02

### Added

- Initial `compliance-kb-mcp` package scaffold.
- Strict Pydantic v2 schemas for framework and control models.
- Static SOC 2 control catalog (61 controls).
- `lookup_control`, `search_controls`, and `list_controls` MCP tools.
- FastMCP stdio server entrypoint.
- Pytest coverage for schema lock-down and tool behavior.
- Local v0.1.0 baseline artifacts for Sprint 1 readiness.

[Unreleased]: https://github.com/Tharanitharan-M/auditpilot/compare/compliance-kb-mcp-v0.2.0...HEAD
[0.2.0]: https://github.com/Tharanitharan-M/auditpilot/compare/compliance-kb-mcp-v0.1.0...compliance-kb-mcp-v0.2.0
[0.1.0]: https://github.com/Tharanitharan-M/auditpilot/releases/tag/compliance-kb-mcp-v0.1.0
