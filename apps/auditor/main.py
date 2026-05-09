"""AuditPilot AdversarialAuditor service (Sprint 8).

Three responsibilities:
  1. Serve the signed AgentCard at ``/.well-known/agent.json`` so the
     orchestrator can discover us (chunk 8.3).
  2. Accept JSON-RPC 2.0 ``SendMessage`` and ``GetTask`` calls at
     ``/a2a`` to run the AdversarialAuditor agent (chunk 8.5).
  3. Expose ``/health`` for the Cloud Run probe (chunk 8.1).

The agent runs in-process per-request. Tasks are stored in an in-memory
dict keyed by task id — Cloud Run keeps a request alive until completion,
so the orchestrator's polling client can fetch results from the same
container without an external task store. We log a warning if the dict
grows past 1000 entries; long-lived deployments should add a TTL eviction.

Refs: PLAN.md Sprint 8 chunks 8.1, 8.2, 8.3, 8.5; ADR-0002.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic_ai import Agent

from apps.auditor.a2a import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Artifact,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    Message,
    Part,
    Task,
    TaskStatus,
    sign_agent_card,
)
from apps.auditor.agents.adversarial import (
    AdversarialResult,
    build_adversarial_agent,
    run_adversarial,
)
from apps.auditor.agents.factory import build_model
from apps.auditor.config import AuditorSettings, get_settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# JSON-RPC 2.0 error codes per spec.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

PROMPT_DIR_ENV = "AUDITOR_PROMPT_DIR"
DEFAULT_PROMPT_NAME = "adversarial"
DEFAULT_PROMPT_LABEL = "production"


def _require_shared_secret(request: Request) -> None:
    """Reject requests without the configured ``A2A_SHARED_SECRET``.

    A static shared secret on the ``Authorization: Bearer ...`` header is
    sufficient inside a VPC for Sprint 8. Sprint 11 hardening replaces this
    with mTLS / OIDC tokens. When ``a2a_shared_secret`` is unset (typical
    in development) the guard is a no-op so local tests work without
    extra setup, but a warning is logged on first hit so the operator
    knows the endpoint is unauthenticated.
    """

    settings = get_settings()
    if settings.a2a_shared_secret is None:
        # Allow only when running in development (matches the api side).
        if settings.environment == "development":
            return
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="A2A_SHARED_SECRET required in non-development environments",
        )
    expected = settings.a2a_shared_secret.get_secret_value()
    auth_header = request.headers.get("authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )
    presented = auth_header[7:].strip()
    # Constant-time comparison so an attacker cannot time-side-channel the secret.
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
        )


def _load_local_prompt(name: str = DEFAULT_PROMPT_NAME, label: str = DEFAULT_PROMPT_LABEL) -> str:
    """Load the system prompt body from the local YAML.

    The auditor mirrors the api's prompt fall-back path. When the api
    package is on PYTHONPATH (the docker image installs it) we use its
    prompts dir; otherwise tests can point ``AUDITOR_PROMPT_DIR`` at a
    fixture dir.
    """

    override = os.environ.get(PROMPT_DIR_ENV)
    if override:
        candidates = [Path(override)]
    else:
        # Resolve apps/api/agents/prompts relative to the repo root so the
        # auditor doesn't import the api package at runtime.
        here = Path(__file__).resolve()
        repo_root = here.parents[2]
        candidates = [repo_root / "apps" / "api" / "agents" / "prompts"]
    for prompts_dir in candidates:
        path = prompts_dir / name / f"{label}.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text())
            if not isinstance(data, dict):
                raise RuntimeError(f"prompt YAML at {path} must be a mapping")
            system = data.get("system")
            if not isinstance(system, str) or not system.strip():
                raise RuntimeError(f"prompt YAML at {path} missing 'system' string")
            return system
    raise RuntimeError(
        f"adversarial prompt not found in any of: {[str(c) for c in candidates]}"
    )


# ── Task store (in-memory) ───────────────────────────────────────────────────


MAX_TASK_STORE_SIZE = 2000
TERMINAL_TASK_TTL_S = 600.0  # evict completed tasks after 10 minutes


class TaskStoreFull(Exception):
    """Raised by ``_TaskStore.create`` when the in-memory cap is hit."""


class _TaskStore:
    """In-memory dict + asyncio lock with hard cap and TTL eviction.

    Sprint 8 ships a single auditor instance, so an in-memory store is
    sufficient. Production hardening: hard ``MAX_TASK_STORE_SIZE`` cap
    and TTL eviction of terminal tasks bound the resource cost so an
    unauthenticated DoS cannot OOM the container. The lock is created
    lazily on first async use so the store can be constructed at
    module-import time before the event loop exists.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[Task, float]] = {}
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _evict_expired(self, now: float) -> None:
        terminal = {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
            "TASK_STATE_BUDGET_EXCEEDED",
        }
        stale = [
            tid
            for tid, (task, recorded_at) in self._store.items()
            if task.status.state in terminal
            and (now - recorded_at) > TERMINAL_TASK_TTL_S
        ]
        for tid in stale:
            del self._store[tid]

    async def create(self, *, context_id: str, history: list[Message]) -> Task:
        task = Task(
            id=str(uuid.uuid4()),
            context_id=context_id,
            status=TaskStatus(state="TASK_STATE_SUBMITTED"),
            history=list(history),
        )
        async with self._get_lock():
            now = time.monotonic()
            self._evict_expired(now)
            if len(self._store) >= MAX_TASK_STORE_SIZE:
                logger.warning(
                    "auditor.task_store.full size=%d max=%d",
                    len(self._store),
                    MAX_TASK_STORE_SIZE,
                )
                raise TaskStoreFull(
                    f"task store at {len(self._store)} of {MAX_TASK_STORE_SIZE}"
                )
            self._store[task.id] = (task, now)
        return task

    async def get(self, task_id: str) -> Task | None:
        async with self._get_lock():
            entry = self._store.get(task_id)
            return entry[0] if entry else None

    async def update(self, task_id: str, **changes: Any) -> Task | None:
        async with self._get_lock():
            entry = self._store.get(task_id)
            if entry is None:
                return None
            current, _ = entry
            updated = current.model_copy(update=changes)
            self._store[task_id] = (updated, time.monotonic())
            return updated


task_store = _TaskStore()


# ── App lifespan ─────────────────────────────────────────────────────────────


_agent_singleton: Agent[None, str] | None = None
_agent_card: AgentCard | None = None


def _build_agent(settings: AuditorSettings) -> Agent[None, str]:
    model = build_model(settings.adversarial_model, settings)
    system_prompt = _load_local_prompt()
    return build_adversarial_agent(model, system_prompt=system_prompt)


def _build_agent_card(settings: AuditorSettings) -> AgentCard:
    base_url = settings.auditor_public_url.rstrip("/")
    card = AgentCard(
        id="urn:auditpilot:adversarial",
        name="AuditPilot AdversarialAuditor",
        version="0.1.0",
        description=(
            "Internal adversarial pass over a draft readiness assessment. "
            "Returns severity-ranked objections for human review."
        ),
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="readiness-redteam",
                name="Readiness Red-Team",
                description=(
                    "Challenge a draft SOC 2 readiness assessment by raising "
                    "the strongest counter-arguments a sceptical reviewer "
                    "would surface."
                ),
                tags=["soc2", "readiness", "red-team"],
                examples=[
                    "Find weaknesses in a draft control_map for CC6.1 across 3 repos",
                ],
            ),
        ],
        interfaces=[
            AgentInterface(transport="JSONRPC", url=f"{base_url}/a2a"),
        ],
    )
    if settings.a2a_private_key is not None:
        try:
            card = sign_agent_card(card, settings.a2a_private_key.get_secret_value())
        except ValueError:
            logger.exception("auditor.card.sign_failed — serving unsigned card")
    else:
        logger.warning(
            "auditor.card.unsigned A2A_PRIVATE_KEY not set — orchestrator will reject in prod"
        )
    return card


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    global _agent_singleton, _agent_card
    settings = get_settings()
    try:
        _agent_singleton = _build_agent(settings)
    except Exception:  # noqa: BLE001
        logger.exception("auditor.agent.build_failed")
        _agent_singleton = None
    try:
        _agent_card = _build_agent_card(settings)
    except Exception:  # noqa: BLE001
        logger.exception("auditor.agent_card.build_failed")
        _agent_card = None
    logger.info(
        "auditor.startup environment=%s budget_cap=%.2f signed=%s",
        settings.environment,
        settings.effective_budget_cap,
        _agent_card is not None and _agent_card.signature is not None,
    )
    yield


app = FastAPI(
    title="auditpilot-auditor",
    version="0.1.0",
    description="AdversarialAuditor service — A2A v1.0",
    lifespan=lifespan,
)


# ── /health ──────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    """Public liveness probe. No build metadata to avoid info disclosure."""
    return {"status": "ok", "service": "auditor"}


@app.get("/internal/health")
async def internal_health(request: Request) -> dict[str, str]:
    """Diagnostic health surface — returns env + git_sha. Requires shared secret."""
    _require_shared_secret(request)
    settings = get_settings()
    return {
        "status": "ok",
        "service": "auditor",
        "version": "0.1.0",
        "environment": settings.environment,
        "git_sha": settings.git_sha,
    }


# ── /.well-known/agent.json ─────────────────────────────────────────────────


@app.get("/.well-known/agent.json")
async def agent_card_endpoint() -> JSONResponse:
    if _agent_card is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AgentCard not initialised",
        )
    return JSONResponse(_agent_card.model_dump(by_alias=True, exclude_none=True))


# ── /a2a (JSON-RPC 2.0) ─────────────────────────────────────────────────────


def _rpc_error(
    code: int,
    message: str,
    *,
    req_id: Any = None,
    data: dict | None = None,
) -> JSONResponse:
    response = JsonRpcResponse(
        id=req_id,
        error=JsonRpcError(code=code, message=message, data=data),
    )
    return JSONResponse(response.model_dump(exclude_none=True))


def _rpc_result(result: dict, *, req_id: Any) -> JSONResponse:
    response = JsonRpcResponse(id=req_id, result=result)
    return JSONResponse(response.model_dump(exclude_none=True))


@app.post("/a2a")
async def jsonrpc_endpoint(request: Request) -> JSONResponse:
    _require_shared_secret(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _rpc_error(JSONRPC_PARSE_ERROR, "Invalid JSON")

    if not isinstance(body, dict):
        return _rpc_error(JSONRPC_INVALID_REQUEST, "Request must be a JSON object")

    req_id = body.get("id")
    try:
        rpc = JsonRpcRequest.model_validate(body)
    except Exception as exc:  # noqa: BLE001
        return _rpc_error(
            JSONRPC_INVALID_REQUEST,
            f"Invalid JSON-RPC request: {exc}",
            req_id=req_id,
        )

    if rpc.method == "SendMessage":
        return await _handle_send_message(rpc)
    if rpc.method == "GetTask":
        return await _handle_get_task(rpc)
    return _rpc_error(JSONRPC_METHOD_NOT_FOUND, f"Unknown method: {rpc.method}", req_id=rpc.id)


async def _handle_send_message(rpc: JsonRpcRequest) -> JSONResponse:
    if _agent_singleton is None:
        return _rpc_error(
            JSONRPC_INTERNAL_ERROR,
            "AdversarialAuditor agent not initialised",
            req_id=rpc.id,
        )
    params = rpc.params or {}
    message_raw = params.get("message")
    if not isinstance(message_raw, dict):
        return _rpc_error(JSONRPC_INVALID_PARAMS, "params.message required", req_id=rpc.id)
    try:
        message = Message.model_validate(message_raw)
    except Exception as exc:  # noqa: BLE001
        return _rpc_error(JSONRPC_INVALID_PARAMS, f"Invalid message: {exc}", req_id=rpc.id)

    scan_context = _extract_scan_context(message)
    if scan_context is None:
        return _rpc_error(
            JSONRPC_INVALID_PARAMS,
            "message.parts must contain a part with media_type 'application/json' "
            "and a 'data' field carrying the scan context",
            req_id=rpc.id,
        )

    settings = get_settings()
    cap = settings.effective_budget_cap
    context_id = str(params.get("contextId", message.message_id))

    try:
        task = await task_store.create(context_id=context_id, history=[message])
    except TaskStoreFull:
        return _rpc_error(
            JSONRPC_INTERNAL_ERROR,
            "Auditor task store at capacity — try again shortly",
            req_id=rpc.id,
        )
    await task_store.update(task.id, status=TaskStatus(state="TASK_STATE_WORKING"))

    with tracer.start_as_current_span("adversarial.run") as span:
        span.set_attribute("auditor.task_id", task.id)
        span.set_attribute("auditor.cap_usd", cap)
        span.set_attribute("auditor.scan_context_keys", len(scan_context))
        started = time.monotonic()
        result = await run_adversarial(
            agent=_agent_singleton,
            scan_context=scan_context,
            cap_usd=cap,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        span.set_attribute("auditor.latency_ms", elapsed_ms)
        span.set_attribute("auditor.findings_count", len(result.findings))
        span.set_attribute("auditor.status", result.status)
    state = _result_to_task_state(result)

    artifact = _result_to_artifact(result)
    refreshed = await task_store.update(
        task.id,
        status=TaskStatus(state=state, error=result.error),
        artifacts=[artifact],
    )
    if refreshed is None:
        # The task store was wiped or evicted between create and update.
        # Should be impossible under MAX_TASK_STORE_SIZE + TTL, but guard
        # so production crashes return a clean JSON-RPC error.
        return _rpc_error(
            JSONRPC_INTERNAL_ERROR,
            "task vanished after update",
            req_id=rpc.id,
        )
    logger.info(
        "auditor.task.completed task_id=%s state=%s findings=%d elapsed_ms=%d",
        task.id,
        state,
        len(result.findings),
        elapsed_ms,
    )
    return _rpc_result(refreshed.model_dump(by_alias=True, exclude_none=True), req_id=rpc.id)


async def _handle_get_task(rpc: JsonRpcRequest) -> JSONResponse:
    params = rpc.params or {}
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id:
        return _rpc_error(JSONRPC_INVALID_PARAMS, "params.id required", req_id=rpc.id)
    task = await task_store.get(task_id)
    if task is None:
        return _rpc_error(JSONRPC_INVALID_PARAMS, "Task not found", req_id=rpc.id)
    return _rpc_result(task.model_dump(by_alias=True, exclude_none=True), req_id=rpc.id)


def _extract_scan_context(message: Message) -> dict[str, Any] | None:
    """Pull the structured scan-context payload out of the message parts."""
    for part in message.parts:
        if part.media_type == "application/json" and isinstance(part.data, dict):
            return part.data
    # Fallback: a single text part containing JSON.
    for part in message.parts:
        if part.text:
            try:
                decoded = json.loads(part.text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    return None


def _result_to_task_state(result: AdversarialResult) -> str:
    if result.status == "completed":
        return "TASK_STATE_COMPLETED"
    if result.status == "budget_exceeded":
        return "TASK_STATE_BUDGET_EXCEEDED"
    return "TASK_STATE_FAILED"


def _result_to_artifact(result: AdversarialResult) -> Artifact:
    return Artifact(
        artifact_id=str(uuid.uuid4()),
        name="adversarial-result",
        parts=[
            Part(
                media_type="application/json",
                data=result.model_dump(),
            )
        ],
    )


__all__ = ["app", "task_store"]
