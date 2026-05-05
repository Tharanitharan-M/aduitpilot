"""
AuditPilot API — FastAPI entrypoint
====================================
Initialises PostHog (if API key configured), mounts the SSE chat bridge,
and exposes a /health probe.

Refs: PLAN.md chunks 2.1, 2.7, 2.13, 2.14, 3.7, 3.8; ADR-0003, ADR-0009, ADR-0014.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from posthog import Posthog
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from apps.api.agents.prompts import PromptLoader
from apps.api.checkpointer import memory_checkpointer
from apps.api.config import Settings
from apps.api.db import close_pool, init_pool
from apps.api.graph import build_graph
from apps.api.jobs import (
    JobQueue,
    JobType,
    RedisLike,
    make_dispatcher,
    make_redis_client,
    reclaim_stale_messages,
    run_worker,
)
from apps.api.observability.langfuse import (
    init_langfuse,
    shutdown_langfuse,
    traced_chat,
)
from apps.api.observability.metrics import (
    init_metrics,
    record_chat_request,
    record_job_processed,
    shutdown_metrics,
)
from apps.api.observability.posthog import (
    capture_event,
    capture_exception,
    init_posthog,
    make_observability_hook,
    shutdown_posthog,
)
from apps.api.routes import connectors_router
from apps.api.sse.ai_sdk_v6 import (
    AI_SDK_V6_HEADER,
    AI_SDK_V6_VERSION,
    ui_message_stream_from_graph_updates,
)

logger = logging.getLogger(__name__)
posthog_client: Posthog | None = None
prompt_loader: PromptLoader | None = None

# Background workers. The lifespan owns these; tests can monkeypatch
# ``_job_queue_factory`` / ``_redis_client_factory`` to route around Redis.
_background_tasks: list[asyncio.Task[Any]] = []
_redis_client: RedisLike | None = None
_job_queue: JobQueue | None = None


def _redis_client_factory(settings: Settings) -> RedisLike:
    return make_redis_client(settings)


def _job_queue_factory(redis: RedisLike) -> JobQueue:
    return JobQueue(redis)


async def _noop_questionnaire_fill(message: Any) -> None:
    logger.info("job.handler.stub questionnaire.fill user_id=%s", message.user_id)


async def _noop_policy_finalize(message: Any) -> None:
    logger.info("job.handler.stub policy.finalize user_id=%s", message.user_id)


async def _noop_mock_audit_run(message: Any) -> None:
    logger.info("job.handler.stub mock_audit.run user_id=%s", message.user_id)


async def _noop_drift_scan(message: Any) -> None:
    logger.info("job.handler.stub drift.scan user_id=%s", message.user_id)


async def _noop_evidence_compact(message: Any) -> None:
    logger.info("job.handler.stub evidence.compact user_id=%s", message.user_id)


def _build_default_handlers() -> dict[JobType, Any]:
    """Sprint 2 handlers are logging stubs.

    Chunk 5.x (evidence-store-mcp wiring), 6.x (policy export), 7.x
    (questionnaire), 8.x (AdversarialAuditor), and 9.x (drift-watcher)
    replace these one at a time. Keeping the registry in ``main`` means
    the Sprint-4 orchestrator can see the full job-type surface today
    without inventing no-op handlers of its own.
    """

    return {
        JobType.QUESTIONNAIRE_FILL: _noop_questionnaire_fill,
        JobType.POLICY_FINALIZE: _noop_policy_finalize,
        JobType.MOCK_AUDIT_RUN: _noop_mock_audit_run,
        JobType.DRIFT_SCAN: _noop_drift_scan,
        JobType.EVIDENCE_COMPACT: _noop_evidence_compact,
    }


_job_handlers_factory = _build_default_handlers


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def _init_posthog(settings: Settings) -> None:
    global posthog_client
    posthog_client = init_posthog(settings)


def _init_prompt_loader(settings: Settings) -> None:  # noqa: ARG001
    """Construct the shared :class:`PromptLoader` for agent modules to consume."""

    global prompt_loader
    hook = make_observability_hook(posthog_client)
    # Langfuse client is wired into the loader lazily: if the Langfuse
    # exporter is up, ``init_langfuse`` has already created a process-
    # global singleton we can grab; otherwise we pass ``None`` and the
    # loader runs in local-YAML-only mode.
    try:
        from langfuse import Langfuse, get_client  # type: ignore[attr-defined]
        try:
            lf_client: Langfuse | None = get_client()  # v4 singleton accessor
        except Exception:  # noqa: BLE001
            lf_client = None
    except Exception:  # noqa: BLE001
        lf_client = None
    prompt_loader = PromptLoader(lf_client, observability_hook=hook)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _redis_client, _job_queue

    settings = get_settings()
    # Pydantic AI's Google/Gemini integration reads ``GOOGLE_API_KEY`` from the
    # process environment; Settings uses ``GEMINI_API_KEY`` (ADR-0008 naming).
    if "GOOGLE_API_KEY" not in os.environ:
        os.environ["GOOGLE_API_KEY"] = settings.gemini_api_key.get_secret_value()
    _init_posthog(settings)
    init_langfuse(settings)
    init_metrics(settings)
    _init_prompt_loader(settings)
    # Application DB pool (Sprint 3.5 chunk 3.5.3). No-op when DATABASE_URL
    # is unset so the dev/test path still boots.
    try:
        await init_pool(settings)
    except Exception:  # noqa: BLE001
        logger.exception("db.pool.init_failed — DB-backed routes will return 503")
    capture_event(
        posthog_client,
        "api_started",
        properties={"version": "0.1.0", "environment": settings.environment},
    )

    try:
        _redis_client = _redis_client_factory(settings)
        _job_queue = _job_queue_factory(_redis_client)
        await _job_queue.ensure_group()
        base_dispatcher = make_dispatcher(_job_handlers_factory())

        async def metered_dispatcher(message):  # type: ignore[no-untyped-def]
            job_type = (
                message.type.value
                if hasattr(message.type, "value")
                else str(message.type)
            )
            try:
                await base_dispatcher(message)
            except Exception:
                record_job_processed(job_type=job_type, status="failed")
                raise
            record_job_processed(job_type=job_type, status="succeeded")

        _background_tasks.append(
            asyncio.create_task(
                run_worker(_job_queue, metered_dispatcher),
                name="auditpilot.worker",
            )
        )
        _background_tasks.append(
            asyncio.create_task(
                reclaim_stale_messages(_job_queue, metered_dispatcher),
                name="auditpilot.reclaim",
            )
        )
        logger.info(
            "background_tasks.started count=%d", len(_background_tasks)
        )
    except Exception:
        logger.exception(
            "background_tasks.start_failed — /chat will still work, jobs will not"
        )

    try:
        yield
    finally:
        for task in _background_tasks:
            task.cancel()
        for task in _background_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        _background_tasks.clear()

        if _redis_client is not None:
            try:
                await _redis_client.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("redis.close_failed")
            _redis_client = None
            _job_queue = None

        # Close the application DB pool (Sprint 3.5 chunk 3.5.3).
        await close_pool()

        await shutdown_langfuse()
        capture_event(
            posthog_client,
            "api_shutdown",
            properties={"version": "0.1.0", "environment": settings.environment},
        )
        shutdown_posthog(posthog_client)
        shutdown_metrics()


def get_job_queue() -> JobQueue:
    """Dependency injector: return the live job queue.

    Raises ``RuntimeError`` if called before ``lifespan`` starts the
    worker — useful for tests that mount the app without lifespan so they
    catch accidental enqueue calls.
    """

    if _job_queue is None:
        raise RuntimeError("JobQueue not initialised; app lifespan has not started")
    return _job_queue


# ──────────────────────────────────────────────────────────────────────────────
# Rate limiting (Sprint 3 day-0 chunk 3.0a)
# ──────────────────────────────────────────────────────────────────────────────
# OWASP LLM10 — Unbounded Consumption. /chat is unauthenticated until Sprint 3
# chunk 3.5 wires Clerk JWT verification, so without this limiter any caller
# could drive unbounded Gemini API spend. Per-IP keying via
# ``get_remote_address`` is the right surrogate for "per-user" until auth lands.
# Limit string read from env (default 10/minute) so tests can override to a
# tight budget.
def _chat_rate_limit() -> str:
    return os.environ.get("CHAT_RATE_LIMIT", "10/minute")


limiter = Limiter(key_func=get_remote_address, default_limits=[])

app = FastAPI(
    title="AuditPilot API",
    version="0.1.0",
    description="Readiness reference architecture — orchestration backend",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.include_router(connectors_router)


@app.middleware("http")
async def _posthog_exception_middleware(request: Request, call_next):
    """Route-level unhandled-exception capture into PostHog (ADR-0014).

    Runs before FastAPI's built-in exception handling. Client-raised
    ``HTTPException`` is not caught here — only server-side failures.
    """

    try:
        return await call_next(request)
    except Exception as exc:  # noqa: BLE001
        capture_exception(
            posthog_client,
            exc,
            properties={
                "request_path": request.url.path,
                "request_method": request.method,
            },
        )
        raise


# ──────────────────────────────────────────────────────────────────────────────
# /health
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": "0.1.0",
        "git_sha": settings.git_sha,
    }


if not get_settings().is_production:

    @app.get("/debug/raise-500")
    async def debug_raise_500() -> None:
        """PostHog verification endpoint — raises an unhandled error on purpose.

        Mounted only when ``settings.environment != "production"``. This keeps
        the endpoint usable for verifying PostHog ingestion in dev/staging
        without exposing an unauthenticated 500-on-demand surface in prod.
        """

        capture_event(
            posthog_client,
            "debug_error_triggered",
            properties={"endpoint": "/debug/raise-500"},
        )
        _ = 1 / 0


# ──────────────────────────────────────────────────────────────────────────────
# /chat — AI SDK 6 UIMessage SSE bridge (ADR-0003, chunk 2.7)
# ──────────────────────────────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    """One message in the AI SDK 6 `UIMessage` convention (minimal shape).

    The frontend sends text as a single `parts: [{type: "text", text}]` entry;
    we flatten to a plain content string for the Python-side LangGraph state.
    """

    model_config = ConfigDict(extra="allow")

    role: Literal["user", "assistant", "system"]
    content: str | None = None
    parts: list[dict[str, Any]] = Field(default_factory=list)


class ChatRequest(BaseModel):
    """POST /chat body shape.

    ``messages`` is the running conversation history (AI SDK 6's `useChat`
    posts the full transcript every turn). ``thread_id`` pins the checkpoint
    row so resume-from-HITL works (Sprint 6 chunk 6.2).
    """

    model_config = ConfigDict(extra="ignore")

    messages: list[ChatMessage]
    thread_id: str | None = None
    intent: (
        Literal["free_chat", "run_readiness_scan", "draft_policy", "fill_questionnaire"]
        | None
    ) = "free_chat"


def _flatten_content(msg: ChatMessage) -> str:
    """Pull text content out of either `.content` or the AI SDK 6 `.parts` array."""
    if msg.content:
        return msg.content
    for p in msg.parts or []:
        if p.get("type") == "text":
            return str(p.get("text", ""))
    return ""


def _to_langchain_messages(body_messages: list[ChatMessage]) -> list:
    """Translate AI SDK 6 wire messages into LangChain Human/AI/System messages."""
    out = []
    for m in body_messages:
        text = _flatten_content(m)
        if m.role == "user":
            out.append(HumanMessage(content=text))
        elif m.role == "assistant":
            out.append(AIMessage(content=text))
        else:  # system
            out.append(SystemMessage(content=text))
    return out


# Model injection lets tests supply FunctionModel; production uses the settings
# + LiteLLM + PromptLoader stack (wired in chunk 2.12).
def _default_model() -> str:
    """Return the default Pydantic AI model string for /chat.

    Sprint 2 keeps this minimal — the orchestrator stub only uses the model
    for the summary turn after lookup_control returns. LiteLLM routing and
    prompt-managed models arrive in chunk 2.12 / Sprint 4.
    """
    return os.environ.get("ORCHESTRATOR_MODEL", "gemini-2.5-flash-lite")


# Module-level hook tests can monkeypatch.
_chat_model_factory = _default_model
_chat_checkpointer_factory = memory_checkpointer


async def _chat_stream_generator(
    *,
    req: ChatRequest,
    thread_id: str,
) -> AsyncIterator[str]:
    """Open a Langfuse observation and stream SSE out of the graph.

    The Langfuse trace is opened BEFORE the stream starts and closed AFTER
    the last chunk. That keeps the whole invocation — including orchestrator
    tool calls and any adversarial dispatch — inside one trace id, so the
    deeplink returned in the `finish` chunk leads to a complete trace.
    """

    lc_messages = _to_langchain_messages(req.messages)
    checkpointer = _chat_checkpointer_factory()
    graph = build_graph(checkpointer, model=_chat_model_factory())
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    async with traced_chat(
        thread_id=thread_id,
        intent=req.intent,
    ) as handle:
        async def finish_metadata() -> dict[str, Any] | None:
            # Surface trace context inside the AI SDK 6 finish chunk so the
            # frontend can deeplink operators straight to the Langfuse trace.
            md: dict[str, Any] = {"thread_id": thread_id, "intent": req.intent}
            if handle.trace_id:
                md["trace_id"] = handle.trace_id
            if handle.trace_url:
                md["trace_url"] = handle.trace_url
            return md

        async for chunk in ui_message_stream_from_graph_updates(
            graph,
            input={"messages": lc_messages},
            config=config,
            message_metadata={"thread_id": thread_id, "intent": req.intent},
            finish_metadata_cb=finish_metadata,
        ):
            yield chunk


@app.post("/chat")
@limiter.limit(_chat_rate_limit)
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    """Stream orchestrator output as AI SDK 6 UIMessage SSE.

    Headers:
      Content-Type: text/event-stream
      x-vercel-ai-ui-message-stream: v1  (handshake, required by useChat)

    Body: ChatRequest JSON, matching AI SDK 6's `useChat` POST shape.

    NOTE: Sprint 2 leaves this endpoint unauthenticated. Clerk JWT verification
    wires in at Sprint 3 chunk 3.5 via a FastAPI dependency. Until then, the
    ``@limiter.limit(_chat_rate_limit)`` decorator above caps requests at
    ``CHAT_RATE_LIMIT`` (default ``10/minute``) per remote IP — Sprint 3 day-0
    chunk 3.0a, OWASP LLM10 mitigation.
    """

    thread_id = req.thread_id or f"thread_{uuid.uuid4().hex}"
    record_chat_request(intent=req.intent, outcome="started")
    return StreamingResponse(
        _chat_stream_generator(req=req, thread_id=thread_id),
        media_type="text/event-stream",
        headers={
            AI_SDK_V6_HEADER: AI_SDK_V6_VERSION,
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # defeat nginx/Cloudflare buffering
        },
    )
