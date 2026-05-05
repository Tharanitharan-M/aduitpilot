"""
Langfuse exporter (chunk 2.8)
=============================
Langfuse 4.x is OpenTelemetry-native: instantiating `Langfuse(...)` wires a
span exporter into the process's OTel tracer provider. Every
`tracer.start_as_current_span(...)` call the orchestrator already makes
surfaces as a Langfuse observation without further instrumentation.

Two responsibilities here:
1. `init_langfuse(settings)` — construct the client on FastAPI startup so
   the exporter is alive before the first `/chat` request.
2. `traced_chat(thread_id, user_id)` — async context manager that opens
   the root `chat` span, exposes `.trace_id` + `.trace_url`, and flushes
   when the stream finishes so the trace lands in Langfuse before the
   Cloud Run container has a chance to be reaped.

Refs: PLAN.md chunk 2.8; ADR-0009; ADR-0011; system-design 6.3.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from langfuse import Langfuse

from apps.api.config import Settings


@dataclass
class TracedChat:
    """Handle surfaced to the SSE bridge for trace-id propagation."""

    trace_id: str | None = None
    trace_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


_INITIALISED: Langfuse | None = None


def init_langfuse(settings: Settings) -> Langfuse | None:
    """Instantiate the Langfuse client once at app startup.

    Returns ``None`` when the required keys are the placeholder "fake" keys
    used in tests — we detect this and avoid opening a real exporter that
    would silently fail background flushes.
    """

    global _INITIALISED
    public_key = settings.langfuse_public_key
    secret_key = settings.langfuse_secret_key.get_secret_value()

    # Detect the "fake" keys used in tests and in the 0F.4 ValidationError
    # smoke tests. Starting a real exporter with these burns an HTTP request
    # on every flush and can deadlock shutdown if the fake host hangs.
    if public_key.startswith(("pk-lf-fake", "pk-lf-...")) or secret_key.startswith(
        ("sk-lf-fake", "sk-lf-...")
    ):
        _INITIALISED = None
        return None

    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=settings.langfuse_host,
        environment=settings.environment,
        release=settings.git_sha,
        tracing_enabled=True,
    )
    _INITIALISED = client
    return client


async def shutdown_langfuse() -> None:
    """Flush pending spans + shut the exporter; safe to call when disabled.

    Both ``flush()`` and ``shutdown()`` perform blocking HTTP I/O. Off-load
    to threads so the FastAPI lifespan teardown (which is itself running on
    the event loop) does not stall while the exporter drains.
    """
    global _INITIALISED
    if _INITIALISED is not None:
        client = _INITIALISED
        _INITIALISED = None
        try:
            await asyncio.to_thread(client.flush)
        finally:
            await asyncio.to_thread(client.shutdown)


def _current_client() -> Langfuse | None:
    """Return the initialised Langfuse client or None.

    ``get_client()`` would otherwise auto-initialise from env vars the
    *first* time it is called, which is exactly the behaviour we want to
    avoid in test contexts.
    """
    return _INITIALISED


@asynccontextmanager
async def traced_chat(
    *,
    thread_id: str,
    user_id: str | None = None,
    intent: str | None = None,
) -> AsyncIterator[TracedChat]:
    """Open the root `chat` Langfuse observation and yield a TracedChat handle.

    The handle is mutated inside the ``with`` block so the finish callback
    can read the trace id *after* the observation has been created. When
    Langfuse is disabled (fake keys in tests) we still yield a handle with
    ``trace_id=None`` so the SSE bridge stays branchless.
    """

    handle = TracedChat()
    client = _current_client()

    if client is None:
        yield handle
        return

    metadata = {"thread_id": thread_id, "intent": intent, "user_id": user_id}
    async with _async_observation(
        client,
        name="chat",
        as_type="agent",
    ) as observation:
        # The Langfuse v4 `agent` overload of `start_as_current_observation`
        # does not accept `metadata` as a constructor kwarg; setting it via
        # `.update(...)` inside the with-block is the supported path.
        try:
            observation.update(metadata=metadata)
        except Exception:  # noqa: BLE001 — never crash the chat path on metadata
            pass
        handle.trace_id = client.get_current_trace_id()
        handle.trace_url = (
            client.get_trace_url(trace_id=handle.trace_id)
            if handle.trace_id
            else None
        )
        handle.metadata = metadata
        try:
            yield handle
        finally:
            # Flush here so the trace is in Langfuse by the time the client
            # renders the finish chunk — otherwise the deeplink 404s for a
            # few seconds while the batch worker catches up. ``flush()`` is
            # blocking HTTP I/O, so off-load to a thread to keep the SSE
            # event loop unblocked for other concurrent /chat streams.
            await asyncio.to_thread(client.flush)


@asynccontextmanager
async def _async_observation(
    client: Langfuse, *, name: str, as_type: str
) -> AsyncIterator[Any]:
    """Bridge Langfuse's sync context manager into async code."""
    cm = client.start_as_current_observation(name=name, as_type=as_type)
    span = cm.__enter__()
    try:
        yield span
    finally:
        cm.__exit__(None, None, None)


__all__ = [
    "TracedChat",
    "init_langfuse",
    "shutdown_langfuse",
    "traced_chat",
]
