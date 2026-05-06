"""
GitHub evidence collection — Sprint 5 chunks 5.3–5.7
======================================================
Five async functions that call the GitHub REST API (read-only) to check the
security posture of a scoped repository. Each function returns zero or more
``Evidence`` rows suitable for the orchestrator's ``collect_evidence`` graph
node.

The content_hash for each row is the SHA-256 of the *normalized* API response:
timestamps (created_at, updated_at, pushed_at), ETags, and other volatile
fields are stripped before hashing so that identical security configurations
produce the same hash across repeated scans — enabling the content-hash cache
in Sprint 5 chunk 5.12 and the drift-watcher dedup in Sprint 9 (system-design
§13.2).

Read-only invariant (ADR-0004)
------------------------------
Every function below makes only GET requests. No write API is called. The
GitHub OAuth scopes required are:
  - ``repo:read``  — branch protection, code scanning, secret scanning,
                     Dependabot
  - ``read:org``   — org-level MFA enforcement (check_org_mfa)

Factory function
-----------------
``make_github_evidence_collector(github_token, repo_full_names)`` returns an
``EvidenceCollector`` callable (same signature as ``default_evidence_collector``)
that runs all five checks for each scoped repository in parallel. The token and
name map are baked into the closure so the caller (graph node) passes only
``repo_id`` and ``scan_run_id``.

Refs: PLAN.md Sprint 5 chunks 5.3–5.7; ADR-0004 (read-only); ADR-0015
(repo-scoped reads); system-design.md §3.2, §6.6; US-006.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from opentelemetry import trace

from apps.api.state import Evidence

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ── Type alias (matches apps.api.services.evidence_collector.EvidenceCollector).
#    PEP 695 ``type`` syntax (Python 3.12+).
type EvidenceCollector = Callable[
    ...,
    Awaitable[list[Evidence]],
]

# GitHub REST API base. Pinned so tests can monkeypatch the module attribute.
_GITHUB_API_BASE = "https://api.github.com"

# Keys stripped from GitHub API responses before hashing (system-design §13.2).
_VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "created_at",
        "updated_at",
        "pushed_at",
        "merged_at",
        "closed_at",
        "committed_date",
        "authored_date",
        "timestamp",
        "last_analysis_sha",
        "most_recent_instance",
        "auto_dismissed_at",
        "fixed_at",
        "dismissed_at",
        "published_at",
    }
)


# ── Normalization helpers ─────────────────────────────────────────────────────


def _strip_volatile(obj: Any) -> Any:
    """Recursively remove volatile keys from a JSON-serialisable object."""

    if isinstance(obj, dict):
        return {
            k: _strip_volatile(v)
            for k, v in obj.items()
            if k not in _VOLATILE_KEYS
        }
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def _content_hash(normalized: Any) -> str:
    """SHA-256 of the canonical JSON representation of a normalized payload."""

    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _make_evidence(
    *,
    check_type: str,
    full_name: str,
    normalized: Any,
    status_label: str,
    scan_run_id: str | None,
) -> Evidence:
    """Build one Evidence row from a normalized GitHub API payload."""

    h = _content_hash(normalized)
    base_raw: dict[str, Any] = {
        "check_type": check_type,
        "full_name": full_name,
        "status": status_label,
    }
    raw = (
        {**base_raw, **normalized}
        if isinstance(normalized, dict)
        else {**base_raw, "data": normalized}
    )
    return Evidence(
        id=f"ev_github_{check_type}_{h[:16]}",
        source_type="github",
        source_uri=f"github://{full_name}/{check_type}",
        raw=raw,
        content_hash=h,
        collected_at=datetime.now(UTC),
        scan_run_id=scan_run_id,
    )


# ── GitHub REST API client helper ────────────────────────────────────────────


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ── 5.3 — Branch protection ──────────────────────────────────────────────────


async def check_branch_protection(
    client: httpx.AsyncClient,
    github_token: str,
    full_name: str,
    scan_run_id: str | None = None,
) -> list[Evidence]:
    """Check branch protection on the default branch (chunk 5.3).

    Calls ``GET /repos/{owner}/{repo}`` to get the default branch, then
    ``GET /repos/{owner}/{repo}/branches/{branch}/protection``.

    Returns one Evidence row regardless of whether protection is enabled so
    the control_map always has a GitHub/CC6.7/CC8.1 signal for this repo.
    Sprint 5 chunk 5.18 — per-check OTel span isolates this check's
    latency / failure inside the parent ``github_evidence.collect`` span.
    """

    with tracer.start_as_current_span("github_evidence.branch_protection") as span:
        span.set_attribute("repo.full_name", full_name)

        owner, repo = full_name.split("/", 1)
        headers = _github_headers(github_token)

        # Step 1: get default branch name.
        default_branch = "main"
        try:
            r = await client.get(
                f"{_GITHUB_API_BASE}/repos/{owner}/{repo}",
                headers=headers,
            )
            if r.status_code == 200:
                default_branch = r.json().get("default_branch", "main")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "github_evidence.branch_protection.repo_info_failed repo=%s err=%r",
                full_name,
                exc,
            )
            span.set_attribute("error.repo_info", type(exc).__name__)

        span.set_attribute("repo.default_branch", default_branch)

        # Step 2: fetch branch protection.
        protection_data: dict[str, Any] = {}
        enabled = False
        try:
            r = await client.get(
                f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/branches/{default_branch}/protection",
                headers=headers,
            )
            span.set_attribute("http.status_code", r.status_code)
            if r.status_code == 200:
                enabled = True
                protection_data = r.json()
            elif r.status_code == 404:
                # 404 means protection not configured — not an error.
                protection_data = {"message": "Branch protection not configured"}
            else:
                protection_data = {"status_code": r.status_code, "detail": r.text[:200]}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "github_evidence.branch_protection.failed repo=%s err=%r",
                full_name,
                exc,
            )
            span.set_attribute("error.protection", type(exc).__name__)
            protection_data = {"error": str(exc)[:200]}
        span.set_attribute("protection.enabled", enabled)

    # Normalize: keep only security-relevant fields from the protection response.
    normalized = _strip_volatile({
        "default_branch": default_branch,
        "protection_enabled": enabled,
        "required_status_checks": protection_data.get("required_status_checks"),
        "enforce_admins": (
            protection_data.get("enforce_admins", {}).get("enabled")
            if isinstance(protection_data.get("enforce_admins"), dict)
            else protection_data.get("enforce_admins")
        ),
        "required_pull_request_reviews": bool(
            protection_data.get("required_pull_request_reviews")
        ),
        "restrictions": bool(protection_data.get("restrictions")),
        "required_linear_history": (
            protection_data.get("required_linear_history", {}).get("enabled")
            if isinstance(protection_data.get("required_linear_history"), dict)
            else None
        ),
        "allow_force_pushes": (
            protection_data.get("allow_force_pushes", {}).get("enabled")
            if isinstance(protection_data.get("allow_force_pushes"), dict)
            else None
        ),
        "allow_deletions": (
            protection_data.get("allow_deletions", {}).get("enabled")
            if isinstance(protection_data.get("allow_deletions"), dict)
            else None
        ),
    })

    return [
        _make_evidence(
            check_type="branch-protection",
            full_name=full_name,
            normalized=normalized,
            status_label="passing" if enabled else "failing",
            scan_run_id=scan_run_id,
        )
    ]


# ── 5.4 — Org MFA enforcement ────────────────────────────────────────────────


async def check_org_mfa(
    client: httpx.AsyncClient,
    github_token: str,
    full_name: str,
    scan_run_id: str | None = None,
) -> list[Evidence]:
    """Check org-level two-factor requirement enforcement (chunk 5.4).

    Calls ``GET /orgs/{org}`` and reads ``two_factor_requirement_enabled``.
    Returns empty list when ``owner`` is a personal account (not an org) — the
    API returns 404 in that case, which we swallow gracefully.

    Requires ``read:org`` OAuth scope. Sprint 5 chunk 5.18 — per-check
    OTel span isolates this check inside the parent collect span.
    """

    with tracer.start_as_current_span("github_evidence.org_mfa") as span:
        owner = full_name.split("/", 1)[0]
        span.set_attribute("repo.full_name", full_name)
        span.set_attribute("org.owner", owner)

        headers = _github_headers(github_token)
        mfa_required: bool | None = None
        org_data: dict[str, Any] = {}

        try:
            r = await client.get(
                f"{_GITHUB_API_BASE}/orgs/{owner}",
                headers=headers,
            )
            span.set_attribute("http.status_code", r.status_code)
            if r.status_code == 200:
                body = r.json()
                mfa_required = body.get("two_factor_requirement_enabled")
                org_data = {
                    "org": owner,
                    "two_factor_requirement_enabled": mfa_required,
                    "members_can_create_repositories": body.get(
                        "members_can_create_repositories"
                    ),
                }
                span.set_attribute("org.mfa_required", bool(mfa_required))
            elif r.status_code == 404:
                # Personal account — no org-level MFA. Skip silently.
                span.set_attribute("org.personal_account", True)
                return []
            else:
                org_data = {"org": owner, "status_code": r.status_code, "detail": r.text[:200]}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "github_evidence.org_mfa.failed owner=%s err=%r",
                owner,
                exc,
            )
            span.set_attribute("error.type", type(exc).__name__)
            return []

    normalized = _strip_volatile(org_data)
    status = "passing" if mfa_required else ("failing" if mfa_required is False else "unknown")

    return [
        _make_evidence(
            check_type="org-mfa",
            full_name=f"{owner}/_org",
            normalized=normalized,
            status_label=status,
            scan_run_id=scan_run_id,
        )
    ]


# ── 5.5 — Code scanning (GitHub Advanced Security) ──────────────────────────


async def check_code_scanning(
    client: httpx.AsyncClient,
    github_token: str,
    full_name: str,
    scan_run_id: str | None = None,
) -> list[Evidence]:
    """Check whether GitHub Advanced Security code scanning is enabled (5.5).

    Calls ``GET /repos/{owner}/{repo}/code-scanning/alerts`` to probe whether
    code scanning is configured. Response semantics:
    - 200 → enabled, returns open alert count.
    - 404 with "no analysis found" → GHAS enabled but no scan yet run.
    - 403 / 404 "Advanced Security not enabled" → GHAS disabled.

    Sprint 5 chunk 5.18 — per-check OTel span.
    """

    with tracer.start_as_current_span("github_evidence.code_scanning") as span:
        span.set_attribute("repo.full_name", full_name)

        owner, repo = full_name.split("/", 1)
        headers = _github_headers(github_token)
        enabled = False
        open_alert_count = 0

        try:
            r = await client.get(
                f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/code-scanning/alerts",
                headers=headers,
                params={"state": "open", "per_page": "1"},
            )
            span.set_attribute("http.status_code", r.status_code)
            if r.status_code == 200:
                # Count is in the X-Total-Count header if present, else len(body).
                enabled = True
                total_header = r.headers.get("x-total-count")
                open_alert_count = (
                    int(total_header) if total_header is not None else len(r.json())
                )
            elif r.status_code == 404:
                body = r.json()
                msg = body.get("message", "")
                # "no analysis found" means GHAS is on but no scan has run yet.
                if "no analysis" in msg.lower():
                    enabled = True
                    open_alert_count = 0
                # Otherwise GHAS is genuinely disabled.
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "github_evidence.code_scanning.failed repo=%s err=%r",
                full_name,
                exc,
            )
            span.set_attribute("error.type", type(exc).__name__)
        span.set_attribute("code_scanning.enabled", enabled)
        span.set_attribute("code_scanning.open_alerts", open_alert_count)

    normalized = _strip_volatile({
        "code_scanning_enabled": enabled,
        "open_alert_count": open_alert_count,
    })

    return [
        _make_evidence(
            check_type="code-scanning",
            full_name=full_name,
            normalized=normalized,
            status_label="passing" if enabled else "failing",
            scan_run_id=scan_run_id,
        )
    ]


# ── 5.6 — Secret scanning ────────────────────────────────────────────────────


async def check_secret_scanning(
    client: httpx.AsyncClient,
    github_token: str,
    full_name: str,
    scan_run_id: str | None = None,
) -> list[Evidence]:
    """Check whether secret scanning is enabled and get open alert count (5.6).

    Calls ``GET /repos/{owner}/{repo}/secret-scanning/alerts``.
    - 200 → enabled; count open alerts.
    - 404 with "Secret scanning is disabled" → disabled.

    Sprint 5 chunk 5.18 — per-check OTel span.
    """

    with tracer.start_as_current_span("github_evidence.secret_scanning") as span:
        span.set_attribute("repo.full_name", full_name)

        owner, repo = full_name.split("/", 1)
        headers = _github_headers(github_token)
        enabled = False
        open_alert_count = 0

        try:
            r = await client.get(
                f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/secret-scanning/alerts",
                headers=headers,
                params={"state": "open", "per_page": "1"},
            )
            span.set_attribute("http.status_code", r.status_code)
            if r.status_code == 200:
                enabled = True
                total_header = r.headers.get("x-total-count")
                open_alert_count = (
                    int(total_header) if total_header is not None else len(r.json())
                )
            elif r.status_code == 404:
                # Disabled — not an error worth logging.
                pass
            elif r.status_code == 422:
                # Public repo with secret scanning not available.
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "github_evidence.secret_scanning.failed repo=%s err=%r",
                full_name,
                exc,
            )
            span.set_attribute("error.type", type(exc).__name__)
        span.set_attribute("secret_scanning.enabled", enabled)
        span.set_attribute("secret_scanning.open_alerts", open_alert_count)

    normalized = _strip_volatile({
        "secret_scanning_enabled": enabled,
        "open_alert_count": open_alert_count,
    })

    if enabled and open_alert_count == 0:
        secret_status = "passing"
    elif not enabled:
        secret_status = "failing"
    else:
        secret_status = "partial"
    return [
        _make_evidence(
            check_type="secret-scanning",
            full_name=full_name,
            normalized=normalized,
            status_label=secret_status,
            scan_run_id=scan_run_id,
        )
    ]


# ── 5.7 — Dependabot ────────────────────────────────────────────────────────


async def check_dependabot(
    client: httpx.AsyncClient,
    github_token: str,
    full_name: str,
    scan_run_id: str | None = None,
) -> list[Evidence]:
    """Check Dependabot security updates and open alert count (5.7).

    Two API calls:
    1. ``GET /repos/{owner}/{repo}/vulnerability-alerts`` — 204 if Dependabot
       is enabled, 404 if disabled.
    2. ``GET /repos/{owner}/{repo}/dependabot/alerts`` — fetch open alert count
       (only called when Dependabot is known-enabled from step 1).

    Sprint 5 chunk 5.18 — per-check OTel span.
    """

    with tracer.start_as_current_span("github_evidence.dependabot") as span:
        span.set_attribute("repo.full_name", full_name)

        owner, repo = full_name.split("/", 1)
        headers = _github_headers(github_token)
        dependabot_enabled = False
        auto_remediation = False
        open_alert_count = 0

        # Step 1: check if Dependabot alerts are enabled.
        try:
            r = await client.get(
                f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/vulnerability-alerts",
                headers=headers,
            )
            span.set_attribute("vuln_alerts.status_code", r.status_code)
            if r.status_code == 204:
                dependabot_enabled = True
            elif r.status_code == 404:
                dependabot_enabled = False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "github_evidence.dependabot.vuln_check_failed repo=%s err=%r",
                full_name,
                exc,
            )
            span.set_attribute("error.vuln_check", type(exc).__name__)

        # Step 2: if enabled, count open alerts and check auto-remediation.
        if dependabot_enabled:
            try:
                r = await client.get(
                    f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/dependabot/alerts",
                    headers=headers,
                    params={"state": "open", "per_page": "1"},
                )
                span.set_attribute("alerts.status_code", r.status_code)
                if r.status_code == 200:
                    total_header = r.headers.get("x-total-count")
                    if total_header is not None:
                        open_alert_count = int(total_header)
                    else:
                        open_alert_count = len(r.json())
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "github_evidence.dependabot.alert_count_failed repo=%s err=%r",
                    full_name,
                    exc,
                )
                span.set_attribute("error.alert_count", type(exc).__name__)

            # Step 3: check auto-remediation (security updates).
            try:
                r = await client.get(
                    f"{_GITHUB_API_BASE}/repos/{owner}/{repo}",
                    headers=headers,
                )
                if r.status_code == 200:
                    body = r.json()
                    sa = body.get("security_and_analysis") or {}
                    dep_su = sa.get("dependabot_security_updates") or {}
                    auto_remediation = dep_su.get("status") == "enabled"
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "github_evidence.dependabot.auto_remediation_check_failed repo=%s err=%r",
                    full_name,
                    exc,
                )
                span.set_attribute("error.auto_remediation", type(exc).__name__)
        span.set_attribute("dependabot.enabled", dependabot_enabled)
        span.set_attribute("dependabot.auto_remediation", auto_remediation)
        span.set_attribute("dependabot.open_alerts", open_alert_count)

    normalized = _strip_volatile({
        "dependabot_enabled": dependabot_enabled,
        "auto_remediation_enabled": auto_remediation,
        "open_alert_count": open_alert_count,
    })

    # Status: passing = enabled with no open alerts, failing = disabled,
    # partial = enabled but has open alerts.
    if not dependabot_enabled:
        status_label = "failing"
    elif open_alert_count == 0:
        status_label = "passing"
    else:
        status_label = "partial"

    return [
        _make_evidence(
            check_type="dependabot",
            full_name=full_name,
            normalized=normalized,
            status_label=status_label,
            scan_run_id=scan_run_id,
        )
    ]


# ── Factory ──────────────────────────────────────────────────────────────────


def make_github_evidence_collector(
    *,
    github_token: str,
    repo_full_names: dict[str, str],
) -> EvidenceCollector:
    """Return an ``EvidenceCollector`` with the token + name map baked in.

    The returned coroutine has the same signature as
    ``default_evidence_collector`` (``repo_id``, ``scan_run_id``).
    It fans out all five checks in parallel for the given repo and returns
    the combined evidence list.

    Token and name map are in the closure — they never enter LangGraph state,
    so they are not checkpointed to Postgres.

    Parameters
    ----------
    github_token:
        GitHub OAuth access token (read-only scopes: repo:read, read:org).
    repo_full_names:
        Mapping from ``provider_repo_id`` (numeric string) to GitHub full name
        in ``"owner/repo"`` format. Populated by the ``/chat`` route from
        ``connector_scoped_repos.full_name``.
    """

    async def collector(
        *,
        repo_id: str,
        scan_run_id: str | None = None,
    ) -> list[Evidence]:
        full_name = repo_full_names.get(repo_id)
        if not full_name or "/" not in full_name:
            logger.warning(
                "github_evidence.collector.no_full_name repo_id=%s — skipping",
                repo_id,
            )
            return []

        with tracer.start_as_current_span("github_evidence.collect") as span:
            span.set_attribute("repo.full_name", full_name)
            span.set_attribute("repo.id", repo_id)

            # Reuse one httpx client per repo_id invocation.
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
                headers=_github_headers(github_token),
            ) as client:
                # All five checks run in parallel; per-check failures are
                # isolated via return_exceptions=True downstream (the caller
                # uses asyncio.gather — errors here produce empty lists).
                results = await asyncio.gather(
                    check_branch_protection(client, github_token, full_name, scan_run_id),
                    check_org_mfa(client, github_token, full_name, scan_run_id),
                    check_code_scanning(client, github_token, full_name, scan_run_id),
                    check_secret_scanning(client, github_token, full_name, scan_run_id),
                    check_dependabot(client, github_token, full_name, scan_run_id),
                    return_exceptions=True,
                )

            evidence: list[Evidence] = []
            check_names = [
                "branch-protection",
                "org-mfa",
                "code-scanning",
                "secret-scanning",
                "dependabot",
            ]
            for check_name, outcome in zip(check_names, results, strict=False):
                if isinstance(outcome, BaseException):
                    logger.warning(
                        "github_evidence.check_failed check=%s repo=%s err=%r",
                        check_name,
                        full_name,
                        outcome,
                    )
                    continue
                evidence.extend(outcome)

            span.set_attribute("evidence.count", len(evidence))
            return evidence

    return collector


__all__ = [
    "EvidenceCollector",
    "check_branch_protection",
    "check_code_scanning",
    "check_dependabot",
    "check_org_mfa",
    "check_secret_scanning",
    "make_github_evidence_collector",
]
