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
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from opentelemetry import trace
from posthog import Posthog
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.models import Model
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from apps.api.agents.models import build_model
from apps.api.agents.prompts import PromptLoader
from apps.api.auth.clerk import ClerkUser, verify_clerk_token
from apps.api.checkpointer import memory_checkpointer
from apps.api.config import Settings
from apps.api.db import AppDbPoolOptionalDep, close_pool, init_pool
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
from apps.api.routes import (
    actions_router,
    connectors_router,
    mock_audit_router,
    policies_router,
    questionnaire_router,
)
from apps.api.routes.policies import ResumeRequest, _upsert_policy_draft
from apps.api.services.github_evidence import make_github_evidence_collector
from apps.api.sse.ai_sdk_v6 import (
    AI_SDK_V6_HEADER,
    AI_SDK_V6_VERSION,
    ui_message_stream_from_graph_updates,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
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


def _build_questionnaire_fill_handler():
    """Build the questionnaire.fill handler bound to the live DB pool + storage."""
    from apps.api.db import get_pool_optional
    from apps.api.services.object_storage import get_object_storage
    from apps.api.services.questionnaire_worker import QuestionnaireFillHandler

    storage = get_object_storage(get_settings())
    return QuestionnaireFillHandler(
        pool_factory=get_pool_optional,
        storage=storage,
    )


async def _noop_policy_finalize(message: Any) -> None:
    logger.info("job.handler.stub policy.finalize user_id=%s", message.user_id)


def _build_mock_audit_handler():
    """Wire the AdversarialAuditor A2A client to the mock_audit.run worker."""
    from apps.api.agents.remote_auditor import RemoteA2aAgent
    from apps.api.db import get_pool_optional
    from apps.api.services.mock_audit_worker import MockAuditRunHandler
    from apps.api.services.object_storage import get_object_storage

    settings = get_settings()
    storage = get_object_storage(settings)

    async def _agent_factory() -> RemoteA2aAgent:
        return RemoteA2aAgent(
            base_url=settings.auditor_url,
            expected_public_key_hex=settings.auditor_a2a_public_key,
        )

    return MockAuditRunHandler(
        pool_factory=get_pool_optional,
        storage=storage,
        remote_agent_factory=_agent_factory,
    )


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
        JobType.QUESTIONNAIRE_FILL: _build_questionnaire_fill_handler(),
        JobType.POLICY_FINALIZE: _noop_policy_finalize,
        JobType.MOCK_AUDIT_RUN: _build_mock_audit_handler(),
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
    # Sprint 4 chunk 4.3a — the previous lifespan wrote
    # ``settings.gemini_api_key`` into ``os.environ["GOOGLE_API_KEY"]`` so
    # Pydantic AI's Google integration would pick it up implicitly. That is
    # subprocess-leakage by construction: every spawned process inherits the
    # secret. ``apps.api.agents.models.build_model`` now constructs the
    # matching ``Provider(api_key=...)`` directly from this ``Settings``
    # instance, so no environment write is required. Operators flip providers
    # via ``ORCHESTRATOR_MODEL=anthropic:claude-sonnet-4-6`` in ``.env`` —
    # zero code changes.
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
app.include_router(actions_router)
app.include_router(policies_router)
app.include_router(questionnaire_router)
app.include_router(mock_audit_router)


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

    # extra="ignore" silently drops AI SDK 6 metadata fields (id, createdAt, etc.)
    # that the server does not need. Avoids OWASP LLM01 / prompt-injection by
    # blocking arbitrary client-supplied fields from being retained on the model.
    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant", "system"]
    content: str | None = None
    parts: list[dict[str, Any]] = Field(default_factory=list)


class ChatRequest(BaseModel):
    """POST /chat body shape.

    ``messages`` is the running conversation history (AI SDK 6's `useChat`
    posts the full transcript every turn). ``thread_id`` pins the checkpoint
    row so resume-from-HITL works (Sprint 6 chunk 6.2).

    Sprint 4 chunk 4.4a — ``repo_include_list`` carries the user-chosen
    repo scope. The dashboard reads this from the picker (Sprint 3.5 chunk
    3.5.2) before opening the /chat stream and forwards it here. The
    orchestrator's ``validate_scope`` node refuses ``run_readiness_scan``
    when this list is empty (ADR-0015 default-deny).
    """

    model_config = ConfigDict(extra="ignore")

    messages: list[ChatMessage]
    thread_id: str | None = Field(default=None, max_length=128)
    intent: (
        Literal["free_chat", "run_readiness_scan", "draft_policy", "fill_questionnaire"]
        | None
    ) = "free_chat"
    # Sprint 4 chunk 4.4a — list of GitHub provider_repo_id strings the
    # user has scoped on their connector. The frontend populates this
    # from `/api/connectors/{id}/scoped-repos`.
    #
    # Sprint 4 chunk 4.16 — per-item cap of 64 chars (max GitHub repo id
    # length plus headroom). Pydantic v2's outer ``max_length=500`` only
    # bounds the list; without an inner ``Annotated[str, max_length=…]``
    # a single 1 MB string could slip through and inflate trace payloads.
    # Sprint 4 chunk 4.11 — the /chat handler additionally cross-checks
    # this list against ``connector_scoped_repos`` server-side so a
    # malicious client cannot inject repo ids the user never picked.
    repo_include_list: list[
        Annotated[str, Field(min_length=1, max_length=64)]
    ] = Field(
        default_factory=list,
        description=(
            "GitHub provider_repo_id strings the user has scoped on the "
            "active connector. Required when intent='run_readiness_scan'. "
            "Each entry capped at 64 chars; list capped at 500 entries."
        ),
        max_length=500,
    )
    connector_id: str | None = Field(
        default=None,
        description=(
            "Clerk external_account.id (e.g. 'eac_*') the scan should "
            "operate on. Sprint 4 surfaces this in trace metadata; "
            "Sprint 4 chunk 4.11 cross-checks scope against this id."
        ),
        max_length=64,
    )
    policy_type: (
        Literal["irp", "access_control", "change_management", "vendor_management"]
        | None
    ) = Field(
        default=None,
        description=(
            "Policy type to draft. Required when intent='draft_policy'. "
            "Sprint 6 chunk 6.9."
        ),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Sprint 5 — GitHub token + repo-name helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _fetch_github_oauth_token(
    user_id: str,
    clerk_secret_key: str,
) -> str | None:
    """Fetch the user's GitHub OAuth access token from the Clerk Backend API.

    Calls ``GET /v1/users/{user_id}/oauth_access_tokens/oauth_github``.
    Returns the first active token or ``None`` when the user has not connected
    GitHub or the call fails.

    Read-only invariant (ADR-0004): this only reads the token, never writes.
    Sprint 6 chunk 6.2 will make auth required on /chat; until then this is
    best-effort — callers fall back to the stub collector when None.
    """

    url = f"https://api.clerk.com/v1/users/{user_id}/oauth_access_tokens/oauth_github"
    headers = {
        "Authorization": f"Bearer {clerk_secret_key}",
        "Content-Type": "application/json",
    }
    with tracer.start_as_current_span("chat._fetch_github_oauth_token") as span:
        span.set_attribute("user_id", user_id)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=headers)
            span.set_attribute("http.status_code", r.status_code)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    span.set_attribute("token.found", True)
                    return data[0].get("token")
            elif r.status_code == 404:
                span.set_attribute("token.found", False)
            else:
                logger.warning(
                    "clerk.oauth_token.failed user_id=%s status=%d",
                    user_id,
                    r.status_code,
                )
        except Exception as exc:  # noqa: BLE001
            span.set_attribute("error.type", type(exc).__name__)
            logger.warning("clerk.oauth_token.exception user_id=%s err=%r", user_id, exc)
    return None


async def _create_scan_run(
    *,
    user_id: str,
    connector_id: str | None,
    repo_include_list: list[str],
    pool: Any,
) -> str | None:
    """Insert a ``scan_runs`` row and return its UUID as a string.

    Sprint 5 chunk 5.1 wired the table; this helper ties the chat handler
    to it so every ``run_readiness_scan`` turn produces a queryable run.
    Returns ``None`` on failure so the caller can fall back to a token-
    only flow without breaking the request.
    """

    with tracer.start_as_current_span("chat._create_scan_run") as span:
        span.set_attribute("user_id", user_id)
        span.set_attribute("repo_include_count", len(repo_include_list))
        if connector_id:
            span.set_attribute("connector_id", connector_id)
        try:
            async with pool.connection() as conn:
                await conn.execute(
                    "SELECT set_config('app.current_user_id', %s, true)",
                    (user_id,),
                )
                row = await conn.execute(
                    """
                    INSERT INTO scan_runs
                        (user_id, connector_id, repo_include_list, status)
                    VALUES
                        (%s, %s, %s::text[], 'running')
                    RETURNING id
                    """,
                    (user_id, connector_id, repo_include_list),
                )
                first = await row.fetchone()
                if first is None:
                    return None
                run_id = str(first[0])
                span.set_attribute("scan_run_id", run_id)
                return run_id
        except Exception as exc:  # noqa: BLE001
            span.set_attribute("error.type", type(exc).__name__)
            logger.warning("scan_runs.create_failed user_id=%s err=%r", user_id, exc)
            return None


async def _complete_scan_run(
    *,
    scan_run_id: str | None,
    user_id: str | None,
    pool: Any | None,
    status: str,
) -> None:
    """Mark a ``scan_runs`` row as completed / failed / cancelled.

    Best-effort. ``status`` must be one of the values allowed by the
    ``scan_runs__status_chk`` CHECK constraint.
    """

    if scan_run_id is None or pool is None or not user_id:
        return
    if status not in {"completed", "failed", "cancelled"}:
        status = "completed"

    try:
        async with pool.connection() as conn:
            await conn.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                (user_id,),
            )
            await conn.execute(
                """
                UPDATE scan_runs
                SET    status       = %s,
                       completed_at = now(),
                       cancelled    = (%s = 'cancelled')
                WHERE  id      = %s::uuid
                  AND  user_id = %s
                """,
                (status, status, scan_run_id, user_id),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scan_runs.complete_failed scan_run_id=%s err=%r",
            scan_run_id,
            exc,
        )


async def _fetch_repo_full_names(
    user_id: str,
    connector_id: str | None,
    repo_include_list: list[str],
    pool: Any | None,
) -> dict[str, str]:
    """Look up provider_repo_id → full_name from connector_scoped_repos.

    Returns an empty dict when the pool is None or the query fails so the
    caller falls back to the stub evidence collector gracefully.
    """

    if not pool or not repo_include_list:
        return {}

    with tracer.start_as_current_span("chat._fetch_repo_full_names") as span:
        span.set_attribute("user_id", user_id)
        span.set_attribute("repo_include_count", len(repo_include_list))
        if connector_id:
            span.set_attribute("connector_id", connector_id)
        try:
            async with pool.connection() as conn:
                await conn.execute(
                    "SELECT set_config('app.current_user_id', %s, true)",
                    (user_id,),
                )
                base_q = """
                    SELECT provider_repo_id, full_name
                    FROM   connector_scoped_repos
                    WHERE  user_id = %s
                      AND  provider_repo_id = ANY(%s::text[])
                """
                params: list[Any] = [user_id, repo_include_list]
                if connector_id:
                    base_q += " AND connector_id = %s"
                    params.append(connector_id)

                rows = await conn.execute(base_q, params)
                result = {row[0]: row[1] async for row in rows}
                span.set_attribute("matched_count", len(result))
                return result
        except Exception as exc:  # noqa: BLE001
            span.set_attribute("error.type", type(exc).__name__)
            logger.warning("repo_full_names.fetch_failed user_id=%s err=%r", user_id, exc)
            return {}


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
# + LiteLLM + PromptLoader stack (wired in chunk 2.12). Sprint 4 chunk 4.3a:
# the factory now returns a fully-constructed ``Model`` instance instead of a
# string, so the provider's API key is threaded explicitly from ``Settings``
# rather than read from the process environment.
def _default_model() -> Model:
    """Return the default Pydantic AI :class:`Model` for /chat.

    Reads ``settings.orchestrator_model`` (default
    ``"google-gla:gemini-2.5-flash-lite"``) and constructs the matching
    provider with the API key wired in from :class:`Settings`. No
    environment-variable mutation: subprocess-leakage closed.

    Refs: PLAN.md Sprint 4 chunk 4.3a; ADR-0001.
    """

    settings = get_settings()
    return build_model(settings.orchestrator_model, settings)


def _default_mcp_toolset() -> bool:
    """Return ``True`` when /chat should attach the live MCP toolset.

    Sprint 4 chunk 4.3: the production /chat path spawns
    ``compliance-kb-mcp`` for every request so tools dispatch over the
    canonical stdio MCP transport. Tests override this hook to ``False``
    so they don't fork a subprocess on every assertion.
    """

    return True


# Module-level hooks tests can monkeypatch.
_chat_model_factory = _default_model
_chat_checkpointer_factory = memory_checkpointer
_chat_mcp_toolset = _default_mcp_toolset


async def _chat_stream_generator(
    *,
    req: ChatRequest,
    thread_id: str,
    request: Request | None = None,
    disconnect_poll_interval_s: float = 5.0,
    # Sprint 5 — optional context for GitHub evidence collection.
    github_token: str | None = None,
    repo_full_names: dict[str, str] | None = None,
    db_pool: Any | None = None,
    gemini_api_key: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[str]:
    """Open a Langfuse observation and stream SSE out of the graph.

    The Langfuse trace is opened BEFORE the stream starts and closed AFTER
    the last chunk. That keeps the whole invocation — including orchestrator
    tool calls and any adversarial dispatch — inside one trace id, so the
    deeplink returned in the `finish` chunk leads to a complete trace.

    Sprint 4 chunk 4.9 — client-disconnect cancellation. We poll
    ``request.is_disconnected()`` every 5 s in a sidecar task while the
    graph generator is running. When the client drops, the sidecar
    cancels the producer and the generator exits cleanly without emitting
    further chunks. The orchestrator's MCP subprocess (and any in-flight
    LLM call) is reaped through Pydantic AI's async-with binding because
    the cancellation propagates through the graph node that opened it.
    """

    lc_messages = _to_langchain_messages(req.messages)
    checkpointer = _chat_checkpointer_factory()

    # Sprint 5: build GitHub evidence collector when a token is available.
    evidence_collector = None
    if github_token and repo_full_names:
        evidence_collector = make_github_evidence_collector(
            github_token=github_token,
            repo_full_names=repo_full_names,
        )

    graph = build_graph(
        checkpointer,
        model=_chat_model_factory(),
        mcp_toolset=_chat_mcp_toolset(),
        evidence_collector=evidence_collector,
        db_pool=db_pool,
        gemini_api_key=gemini_api_key,
    )
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    # Sprint 5 — for run_readiness_scan turns, materialise a row in
    # ``scan_runs`` so the evidence rows have a real foreign key and the
    # ``list_scan_runs`` MCP tool can return real data. Best-effort: a
    # missing pool / DB outage falls back to a UUID-only scan_run_id and
    # the persistence layer keeps working without the FK.
    scan_run_id: str | None = None
    if (
        req.intent == "run_readiness_scan"
        and req.repo_include_list
        and db_pool is not None
        and user_id
    ):
        scan_run_id = await _create_scan_run(
            user_id=user_id,
            connector_id=req.connector_id,
            repo_include_list=list(req.repo_include_list),
            pool=db_pool,
        )

    # Sprint 4 chunks 4.4a/4.4b — seed graph state with scope + intent.
    # Sprint 5 chunk 5.19 — ``repo_full_names`` is intentionally NOT
    # seeded into state; it lives in the collector closure instead so
    # external repo names never enter the LangGraph checkpoint store.
    # Sprint 5 — seed user_id and scan_run_id so the persistence layer
    # and the GitHub collector have what they need.
    graph_input: dict[str, Any] = {
        "messages": lc_messages,
        "intent": req.intent,
        "repo_include_list": list(req.repo_include_list),
        "user_id": user_id,
        "scan_run_id": scan_run_id,
    }
    if req.policy_type:
        graph_input["policy_type"] = req.policy_type

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

        producer = ui_message_stream_from_graph_updates(
            graph,
            input=graph_input,
            config=config,
            message_metadata={"thread_id": thread_id, "intent": req.intent},
            finish_metadata_cb=finish_metadata,
        )

        # Sprint 4 chunk 4.9 — sidecar that watches the client connection.
        # Cancelled when the producer finishes normally; cancels the
        # producer when the client drops first. The sidecar does no work
        # when ``request`` is None (e.g. unit tests that bypass it).
        cancel_event = asyncio.Event()

        async def _disconnect_watcher() -> None:
            if request is None:
                return
            try:
                while not cancel_event.is_set():
                    if await request.is_disconnected():
                        logger.info(
                            "chat.client_disconnected thread_id=%s — "
                            "cancelling graph",
                            thread_id,
                        )
                        record_chat_request(intent=req.intent, outcome="cancelled")
                        cancel_event.set()
                        return
                    await asyncio.sleep(disconnect_poll_interval_s)
            except asyncio.CancelledError:
                # Producer finished first — nothing to clean up.
                raise

        watcher_task = asyncio.create_task(
            _disconnect_watcher(), name="auditpilot.chat.disconnect_watcher"
        )

        # Sprint 5 — track the run outcome so we can flip ``scan_runs.status``
        # on the way out. Defaults to "completed" unless the producer
        # raised, the client disconnected, or we yielded an error chunk.
        run_outcome: str = "completed"

        try:
            async for chunk in producer:
                if cancel_event.is_set():
                    # Client dropped — bail out before the next yield.
                    run_outcome = "cancelled"
                    return
                if '"type":"error"' in chunk or '"type":"abort"' in chunk:
                    run_outcome = "failed"
                yield chunk
        except Exception:
            run_outcome = "failed"
            raise
        finally:
            watcher_task.cancel()
            try:
                await watcher_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            # Best-effort completion — never raise from the finally block.
            try:
                await _complete_scan_run(
                    scan_run_id=scan_run_id,
                    user_id=user_id,
                    pool=db_pool,
                    status=run_outcome,
                )
            except Exception:  # noqa: BLE001
                logger.exception("scan_runs.complete_unexpected_failure")

            # Sprint 6 — persist the policy draft from graph state to
            # the policy_drafts table so the /api/policies endpoint can
            # serve it. Best-effort: DB failure here does not crash the
            # stream (it already finished).
            if (
                req.intent == "draft_policy"
                and db_pool is not None
                and user_id
            ):
                try:
                    final_state = await graph.aget_state(config)
                    dp = (
                        final_state.values.get("draft_policy")
                        if final_state
                        else None
                    )
                    if dp is not None:
                        d = (
                            dp.model_dump()
                            if hasattr(dp, "model_dump")
                            else dp
                        )
                        await _upsert_policy_draft(
                            db_pool,
                            user_id=user_id,
                            draft_id=d["id"],
                            policy_type=d.get(
                                "policy_type", "irp"
                            ),
                            title=d.get("title", ""),
                            content=d.get("content", ""),
                            version=d.get("version", 1),
                            finalized=d.get("finalized", False),
                            thread_id=thread_id,
                        )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "policy_draft.persist_failure "
                        "thread_id=%s",
                        thread_id,
                    )


@app.post("/chat")
@limiter.limit(_chat_rate_limit)
async def chat(
    req: ChatRequest,
    request: Request,
    pool: AppDbPoolOptionalDep,
    clerk_user: ClerkUser = Depends(verify_clerk_token),
) -> StreamingResponse:
    """Stream orchestrator output as AI SDK 6 UIMessage SSE.

    Headers:
      Content-Type: text/event-stream
      x-vercel-ai-ui-message-stream: v1  (handshake, required by useChat)

    Body: ChatRequest JSON, matching AI SDK 6's `useChat` POST shape.

    Auth: Clerk JWT required. Missing or invalid token → HTTP 401.
    The verified user_id is used to fetch the user's GitHub OAuth token
    from the Clerk Backend API (Sprint 5 chunks 5.3-5.7) and to scope
    evidence persistence to the correct tenant (RLS).

    Rate limiting: ``CHAT_RATE_LIMIT`` (default 10/minute) per IP — Sprint 3
    day-0 chunk 3.0a, OWASP LLM10 mitigation.
    """

    thread_id = req.thread_id or f"thread_{uuid.uuid4().hex}"
    record_chat_request(intent=req.intent, outcome="started")

    user_id: str = clerk_user.user_id
    github_token: str | None = None
    repo_full_names: dict[str, str] = {}

    # Fetch GitHub OAuth token and repo names — non-fatal on failure so a
    # missing connector does not break free-chat.
    try:
        settings = get_settings()
        github_token = await _fetch_github_oauth_token(
            user_id,
            settings.clerk_secret_key.get_secret_value(),
        )
        if github_token and req.repo_include_list:
            repo_full_names = await _fetch_repo_full_names(
                user_id,
                req.connector_id,
                list(req.repo_include_list),
                pool,
            )
    except Exception:  # noqa: BLE001
        pass

    # Sprint 4 chunk 4.11 — server-side scope cross-check. The frontend
    # supplies ``repo_include_list``, but we cannot trust it: a malicious
    # or buggy client could request a scan of repos the user never picked
    # on the connector. ``_fetch_repo_full_names`` joins against
    # ``connector_scoped_repos`` with RLS-bound ``user_id``, so its keys
    # are exactly the provider_repo_ids the user owns on this connector.
    # Filter the include list to that intersection. When ``repo_full_names``
    # is empty (no DB pool, no token, no scope) we leave the original list
    # in place so the validate_scope node can still emit its empty-scope
    # refusal message — that path is independent of cross-checking.
    if repo_full_names and req.repo_include_list:
        original_count = len(req.repo_include_list)
        validated = [
            rid for rid in req.repo_include_list if rid in repo_full_names
        ]
        if len(validated) != original_count:
            logger.warning(
                "chat.scope.cross_check_filtered user_id=%s requested=%d retained=%d",
                user_id,
                original_count,
                len(validated),
            )
        # ChatRequest is mutable (extra='ignore', not frozen). Replace the
        # field in-place so every downstream consumer (graph_input,
        # scan_runs row, _chat_stream_generator) sees the cross-checked
        # list rather than the client-supplied one.
        req.repo_include_list = validated

    gemini_key: str | None = None
    try:
        gemini_key = get_settings().gemini_api_key.get_secret_value()
    except Exception:  # noqa: BLE001
        pass

    return StreamingResponse(
        _chat_stream_generator(
            req=req,
            thread_id=thread_id,
            request=request,
            github_token=github_token,
            repo_full_names=repo_full_names,
            db_pool=pool,
            gemini_api_key=gemini_key,
            user_id=user_id,
        ),
        media_type="text/event-stream",
        headers={
            AI_SDK_V6_HEADER: AI_SDK_V6_VERSION,
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# /chat/resume — HITL resume endpoint (Sprint 6 chunk 6.2, ADR-0007)
# ──────────────────────────────────────────────────────────────────────────────


async def _resume_stream_generator(
    *,
    thread_id: str,
    resume_payload: dict[str, Any],
    request: Request | None = None,
    db_pool: Any | None = None,
    gemini_api_key: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[str]:
    """Resume an interrupted graph and stream the result as SSE."""

    from langgraph.types import Command

    checkpointer = _chat_checkpointer_factory()
    graph = build_graph(
        checkpointer,
        model=_chat_model_factory(),
        mcp_toolset=_chat_mcp_toolset(),
        db_pool=db_pool,
        gemini_api_key=gemini_api_key,
    )
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    async with traced_chat(thread_id=thread_id, intent="resume") as handle:
        async def finish_metadata() -> dict[str, Any] | None:
            md: dict[str, Any] = {"thread_id": thread_id, "intent": "resume"}
            if handle.trace_id:
                md["trace_id"] = handle.trace_id
            if handle.trace_url:
                md["trace_url"] = handle.trace_url
            return md

        producer = ui_message_stream_from_graph_updates(
            graph,
            input=Command(resume=resume_payload),
            config=config,
            message_metadata={"thread_id": thread_id, "intent": "resume"},
            finish_metadata_cb=finish_metadata,
        )

        async for chunk in producer:
            yield chunk


@app.post("/chat/resume")
@limiter.limit(_chat_rate_limit)
async def chat_resume(
    req: ResumeRequest,
    request: Request,
    pool: AppDbPoolOptionalDep,
    clerk_user: ClerkUser = Depends(verify_clerk_token),
) -> StreamingResponse:
    """Resume an interrupted HITL graph (Sprint 6 chunk 6.2, ADR-0007).

    Auth: same Clerk JWT as the original /chat. The verified user_id
    must match the user who started the interrupted graph — the
    PostgresSaver checkpoint is keyed by thread_id which is session-
    scoped, and the graph state contains user_id for cross-check.

    Returns an SSE stream with the graph's post-resume output.
    """

    user_id: str = clerk_user.user_id
    record_chat_request(intent="resume", outcome="started")

    # Ownership cross-check: verify the authenticated user owns the
    # interrupted thread before allowing resume (python-reviewer F1).
    checkpointer = _chat_checkpointer_factory()
    ownership_graph = build_graph(
        checkpointer,
        model=_chat_model_factory(),
        mcp_toolset=False,
    )
    ownership_config: dict[str, Any] = {"configurable": {"thread_id": req.thread_id}}
    try:
        state = await ownership_graph.aget_state(ownership_config)
        if state and state.values:
            thread_owner = state.values.get("user_id")
            if thread_owner and thread_owner != user_id:
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail="Thread not owned by this user")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("resume.ownership_check_failed thread_id=%s", req.thread_id)

    resume_payload = {
        "decision": req.decision,
        "edited_content": req.edited_content,
        "rejection_reason": req.rejection_reason,
    }

    gemini_key: str | None = None
    try:
        gemini_key = get_settings().gemini_api_key.get_secret_value()
    except Exception:  # noqa: BLE001
        pass

    return StreamingResponse(
        _resume_stream_generator(
            thread_id=req.thread_id,
            resume_payload=resume_payload,
            request=request,
            db_pool=pool,
            gemini_api_key=gemini_key,
            user_id=user_id,
        ),
        media_type="text/event-stream",
        headers={
            AI_SDK_V6_HEADER: AI_SDK_V6_VERSION,
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
