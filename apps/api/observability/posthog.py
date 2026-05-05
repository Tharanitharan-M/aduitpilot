"""PostHog integration — backend error tracking + server-side events.

Per ADR-0014, PostHog is the single error-tracking + product-analytics +
session-replay tool for AuditPilot. This module owns:

* Client init / shutdown driven from the FastAPI lifespan.
* A FastAPI middleware that captures any unhandled request exception
  into PostHog before re-raising (so ``/health`` and 404s stay silent
  while a real 500 ends up in the PostHog inbox).
* A thin :func:`capture_event` helper callers pass to downstream
  modules (PromptLoader, JobQueue DLQ handlers) so they can emit
  operator-facing observability events without importing PostHog
  directly.

No-op mode: if ``settings.posthog_api_key`` is absent, :func:`init_posthog`
returns ``None`` and :func:`capture_event` becomes a silent no-op. Tests
and the demo path both rely on that.
"""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from fastapi import FastAPI, Request
from posthog import Posthog
from starlette.middleware.base import BaseHTTPMiddleware

from apps.api.config import Settings

logger = logging.getLogger(__name__)

_SERVER_DISTINCT_ID = "server"


def init_posthog(settings: Settings) -> Posthog | None:
    """Return a configured :class:`posthog.Posthog` client or ``None``.

    A no-op client is never returned — the caller is expected to guard
    its own event calls with ``if client is not None`` (or go through
    :func:`capture_event`, which handles the ``None`` case already).
    """

    api_key = settings.posthog_api_key
    if not api_key:
        logger.info("posthog.disabled reason=no-api-key")
        return None

    client = Posthog(
        project_api_key=api_key,
        host=settings.posthog_host,
        enable_exception_autocapture=True,
    )

    # Best-effort shutdown if the process exits without hitting lifespan
    # teardown (e.g. uvicorn --reload during dev).
    atexit.register(lambda: shutdown_posthog(client))
    logger.info("posthog.initialised host=%s", settings.posthog_host)
    return client


def shutdown_posthog(client: Posthog | None) -> None:
    if client is None:
        return
    with suppress(Exception):
        client.flush()
    with suppress(Exception):
        client.shutdown()


def capture_event(
    client: Posthog | None,
    event: str,
    *,
    properties: dict[str, Any] | None = None,
    distinct_id: str | None = None,
) -> None:
    """Fire a server-side event; silent no-op if PostHog is not configured."""

    if client is None:
        return
    with suppress(Exception):
        client.capture(
            distinct_id=distinct_id or _SERVER_DISTINCT_ID,
            event=event,
            properties=properties or {},
        )


def capture_exception(
    client: Posthog | None,
    exc: BaseException,
    *,
    distinct_id: str | None = None,
    properties: dict[str, Any] | None = None,
) -> None:
    """Attach an exception to PostHog with a consistent property shape."""

    if client is None:
        return
    payload = {
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
    }
    if properties:
        payload.update(properties)
    with suppress(Exception):
        client.capture(
            distinct_id=distinct_id or _SERVER_DISTINCT_ID,
            event="$exception",
            properties=payload,
        )


class PostHogExceptionMiddleware(BaseHTTPMiddleware):
    """Capture unhandled exceptions from any route into PostHog.

    The middleware wraps the dispatcher, so it runs *before* FastAPI's
    built-in exception handler converts a 500 into a JSON response. That
    ordering is important: if we captured inside an exception handler,
    client-raised ``HTTPException`` (4xx) would end up in the inbox.
    """

    def __init__(self, app, client: Posthog | None) -> None:
        super().__init__(app)
        self._client = client

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 — by design, we re-raise
            capture_exception(
                self._client,
                exc,
                properties={
                    "request_path": request.url.path,
                    "request_method": request.method,
                },
            )
            raise


def install_middleware(app: FastAPI, client: Posthog | None) -> None:
    """Mount the exception-capture middleware on ``app``."""

    app.add_middleware(PostHogExceptionMiddleware, client=client)


def make_observability_hook(
    client: Posthog | None,
) -> Callable[[str, str, dict[str, Any]], None]:
    """Return a 3-arg observability hook compatible with PromptLoader.

    Maps ``(event_name, subject_name, context) → PostHog capture``. Tests
    that want to assert a fallback event fired can pass the same callable
    and inspect the PostHog mock.
    """

    def _hook(event: str, name: str, context: dict[str, Any]) -> None:
        capture_event(
            client,
            f"auditpilot.{event}",
            properties={"subject": name, **context},
        )

    return _hook
