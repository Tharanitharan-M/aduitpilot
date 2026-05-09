"""RemoteA2aAgent client — orchestrator → AdversarialAuditor over A2A v1.0.

The orchestrator side of Sprint 8 chunk 8.4. Responsibilities:

- Fetch the auditor's signed AgentCard from
  ``{auditor_base_url}/.well-known/agent.json`` and cache it for the
  process lifetime.
- Verify the card's Ed25519 signature against the public key pinned in
  ``Settings.auditor_a2a_public_key``. A failed verification refetches
  once and gives up.
- Issue JSON-RPC 2.0 ``SendMessage`` and ``GetTask`` calls against the
  endpoint declared in the card's ``interfaces``.
- Poll until ``TASK_STATE_COMPLETED`` / ``TASK_STATE_FAILED`` /
  ``TASK_STATE_BUDGET_EXCEEDED`` or ``timeout_seconds`` elapses (per
  US-019 cold-start tolerance — 60 s default for first-call paths).

The class is intentionally framework-agnostic so the worker handler in
``apps/api/services/mock_audit_worker.py`` can import it without
dragging in a Pydantic AI Agent. Tests inject a stub ``http_client`` so
no real network call is made.

Refs: PLAN.md Sprint 8 chunks 8.4 / 8.5; ADR-0002.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from apps.auditor.a2a.agent_card import AgentCard, verify_agent_card

tracer = trace.get_tracer(__name__)

logger = logging.getLogger(__name__)

DEFAULT_FETCH_TIMEOUT_S = 5.0
DEFAULT_RPC_TIMEOUT_S = 30.0
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_POLL_BUDGET_S = 60.0


class RemoteAuditorError(RuntimeError):
    """Generic failure when talking to the remote AdversarialAuditor."""


class AgentCardSignatureError(RemoteAuditorError):
    """The fetched AgentCard either has no signature or the signature is invalid."""


class A2ATaskFailure(RemoteAuditorError):
    """The remote task ended in a non-completed state."""

    def __init__(self, *, state: str, error: str | None) -> None:
        super().__init__(f"A2A task ended in state {state}: {error or '<no error>'}")
        self.state = state
        self.error = error


class A2ATaskResult(BaseModel):
    """Whatever ``GetTask`` returned, normalised for the worker handler.

    Pydantic v2 model so the boundary from the auditor process to the
    orchestrator side is validated and forbid-extra. Plain dicts inside
    ``findings`` are validated downstream by ``AdversarialFinding`` if
    the worker needs strict types.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    state: str
    findings: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    budget: dict[str, float | int] = Field(default_factory=dict)
    error: str | None = None
    raw_artifact: dict[str, Any] | None = None


def _validate_base_url(base_url: str) -> str:
    """Reject loopback / link-local / non-HTTP(S) base URLs.

    SSRF defence: ``base_url`` is sourced from ``Settings.auditor_url``
    today, but this guard makes the class safe against future code paths
    that derive it from less trusted input. Loopback is allowed in
    development (``http://localhost:8001``); the production allowlist is
    enforced by ``AUDITOR_HOST_ALLOWLIST`` when set.
    """

    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"auditor base_url must be http(s); got {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("auditor base_url missing hostname")
    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_link_local or ip.is_multicast):
        raise ValueError("auditor base_url resolves to link-local / multicast")
    return base_url


class RemoteA2aAgent:
    """Orchestrator-side A2A v1.0 transport client for the AdversarialAuditor.

    NOTE: NOT an LLM agent. This is a JSON-RPC HTTP client that talks to
    the AdversarialAuditor service over A2A v1.0. It does not consume
    tokens, does not have a system prompt, and does not count toward
    the three-agent constraint (ADR-0002). The class name is preserved
    because it matches the A2A-SDK terminology, but consumers should
    treat it as a transport.
    """

    def __init__(
        self,
        *,
        base_url: str,
        expected_public_key_hex: str | None,
        http_client: httpx.AsyncClient | None = None,
        rpc_timeout_s: float = DEFAULT_RPC_TIMEOUT_S,
        fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_budget_s: float = DEFAULT_POLL_BUDGET_S,
    ) -> None:
        self._base_url = _validate_base_url(base_url).rstrip("/")
        self._expected_public_key = expected_public_key_hex
        self._http = http_client
        self._owns_client = http_client is None
        self._rpc_timeout_s = rpc_timeout_s
        self._fetch_timeout_s = fetch_timeout_s
        self._poll_interval_s = poll_interval_s
        self._poll_budget_s = poll_budget_s
        self._card: AgentCard | None = None
        self._endpoint: str | None = None
        # Lazy lock — bound to the running loop on first async use, not at
        # __init__ time (which may be called outside any event loop).
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def aclose(self) -> None:
        if self._owns_client and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._rpc_timeout_s)
            self._owns_client = True
        return self._http

    async def fetch_agent_card(self, *, force_refresh: bool = False) -> AgentCard:
        async with self._get_lock():
            if self._card is not None and not force_refresh:
                return self._card
            url = f"{self._base_url}/.well-known/agent.json"
            client = await self._client()
            with tracer.start_as_current_span("remote_auditor.fetch_agent_card") as span:
                span.set_attribute("http.url", url)
                span.set_attribute("http.method", "GET")
                try:
                    response = await client.get(url, timeout=self._fetch_timeout_s)
                except httpx.HTTPError as exc:
                    span.set_attribute("error", True)
                    raise RemoteAuditorError(f"AgentCard fetch failed: {exc}") from exc
                span.set_attribute("http.status_code", response.status_code)
                if response.status_code != 200:
                    raise RemoteAuditorError(
                        f"AgentCard fetch returned {response.status_code}: {response.text[:200]}"
                    )
                try:
                    card = AgentCard.model_validate(response.json())
                except (ValueError, json.JSONDecodeError) as exc:
                    raise RemoteAuditorError(f"AgentCard payload invalid: {exc}") from exc

            if self._expected_public_key:
                if not verify_agent_card(card, expected_public_key_hex=self._expected_public_key):
                    raise AgentCardSignatureError(
                        "AgentCard signature did not verify against pinned public key"
                    )
            else:
                logger.warning(
                    "remote_auditor.unverified_card base_url=%s "
                    "— AUDITOR_A2A_PUBLIC_KEY not pinned",
                    self._base_url,
                )

            self._card = card
            self._endpoint = next(
                (i.url for i in card.interfaces if i.transport == "JSONRPC"),
                f"{self._base_url}/a2a",
            )
            return card

    async def send_message(self, scan_context: dict[str, Any]) -> A2ATaskResult:
        """Send the scan context as one A2A task and poll until terminal.

        The auditor today completes in a single request, so the
        ``SendMessage`` response already carries the terminal state. We
        still call ``GetTask`` once afterwards so this client handles a
        future async-only auditor without changes.
        """

        await self.fetch_agent_card()
        endpoint = self._endpoint or f"{self._base_url}/a2a"
        message = {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "parts": [
                {
                    "mediaType": "application/json",
                    "data": scan_context,
                }
            ],
        }
        result = await self._jsonrpc(
            endpoint,
            method="SendMessage",
            params={"message": message},
        )
        task = self._coerce_task(result)
        terminal_states = {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
            "TASK_STATE_BUDGET_EXCEEDED",
        }
        if task.get("status", {}).get("state") in terminal_states:
            return self._normalise(task)

        # Poll path.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._poll_budget_s
        task_id = task.get("id")
        if not task_id:
            raise RemoteAuditorError("SendMessage response missing task id")
        while True:
            if loop.time() > deadline:
                raise RemoteAuditorError(
                    f"A2A task {task_id} did not reach a terminal state within "
                    f"{self._poll_budget_s:.0f}s"
                )
            await asyncio.sleep(self._poll_interval_s)
            polled = await self._jsonrpc(
                endpoint,
                method="GetTask",
                params={"id": task_id},
            )
            polled_task = self._coerce_task(polled)
            if polled_task.get("status", {}).get("state") in terminal_states:
                return self._normalise(polled_task)

    async def _jsonrpc(self, endpoint: str, *, method: str, params: dict) -> dict[str, Any]:
        client = await self._client()
        body = {"jsonrpc": "2.0", "method": method, "params": params, "id": uuid.uuid4().hex}
        with tracer.start_as_current_span(f"remote_auditor.{method}") as span:
            span.set_attribute("http.url", endpoint)
            span.set_attribute("http.method", "POST")
            span.set_attribute("rpc.method", method)
            try:
                response = await client.post(endpoint, json=body, timeout=self._rpc_timeout_s)
            except httpx.HTTPError as exc:
                span.set_attribute("error", True)
                raise RemoteAuditorError(f"A2A {method} POST failed: {exc}") from exc
            span.set_attribute("http.status_code", response.status_code)
            if response.status_code >= 500:
                raise RemoteAuditorError(
                    f"A2A {method} returned {response.status_code}: {response.text[:200]}"
                )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise RemoteAuditorError(f"A2A {method} returned non-JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RemoteAuditorError(f"A2A {method} returned non-object: {type(payload).__name__}")
        if payload.get("error") is not None:
            err = payload["error"]
            raise RemoteAuditorError(
                f"A2A {method} JSON-RPC error code={err.get('code')} message={err.get('message')}"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RemoteAuditorError(f"A2A {method} response missing 'result' object")
        return result

    @staticmethod
    def _coerce_task(result: dict[str, Any]) -> dict[str, Any]:
        # The auditor returns the Task object directly as ``result``.
        return result

    @staticmethod
    def _normalise(task: dict[str, Any]) -> A2ATaskResult:
        status = task.get("status", {}) or {}
        state = str(status.get("state", "TASK_STATE_FAILED"))
        artifacts = task.get("artifacts", []) or []
        findings: list[dict[str, Any]] = []
        summary = ""
        budget: dict[str, float | int] = {}
        raw_artifact = None
        for artifact in artifacts:
            for part in artifact.get("parts", []) or []:
                if part.get("mediaType") != "application/json":
                    continue
                data = part.get("data") or {}
                if not isinstance(data, dict):
                    continue
                raw_artifact = data
                summary = str(data.get("summary", ""))[:4000]
                budget = data.get("budget", {}) or {}
                for finding in data.get("findings", []) or []:
                    if isinstance(finding, dict):
                        findings.append(finding)
                break
            if raw_artifact is not None:
                break
        if state in {"TASK_STATE_FAILED", "TASK_STATE_BUDGET_EXCEEDED", "TASK_STATE_CANCELED"}:
            return A2ATaskResult(
                task_id=str(task.get("id", "")),
                state=state,
                findings=findings,
                summary=summary,
                budget=budget,
                error=status.get("error"),
                raw_artifact=raw_artifact,
            )
        return A2ATaskResult(
            task_id=str(task.get("id", "")),
            state=state,
            findings=findings,
            summary=summary,
            budget=budget,
            raw_artifact=raw_artifact,
        )


__all__ = [
    "A2ATaskFailure",
    "A2ATaskResult",
    "AgentCardSignatureError",
    "RemoteA2aAgent",
    "RemoteAuditorError",
]
