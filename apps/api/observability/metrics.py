"""Grafana Cloud OTel metrics exporter + AuditPilot custom meters.

Per ADR-0009, infrastructure metrics ship to Grafana Cloud via OTLP over
HTTP/protobuf (the only OTLP transport Grafana's gateway accepts on the
free tier). This module:

* Parses the ``OTEL_EXPORTER_OTLP_HEADERS`` string (the Grafana console
  emits it in ``k1=v1,k2=v2`` form with already-URL-encoded values).
* Installs an :class:`OTLPMetricExporter` bound to a periodic reader.
* Exposes three custom AuditPilot counters / histograms wrapped in thin
  Python functions so callers don't touch the OTel API directly:

  - :func:`record_chat_request` — counter on POST ``/chat`` invocations.
  - :func:`record_job_processed` — counter keyed by ``(job_type, status)``.
  - :func:`record_llm_tokens` — counter for prompt/completion tokens,
    used by the LiteLLM hook in Sprint 4 chunk 4.x.

Environment fallback: if ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset the
exporter is disabled and the helpers become silent no-ops so unit tests
and the $0/month demo path both stay happy.
"""

from __future__ import annotations

import logging
import threading
from typing import Final
from urllib.parse import unquote

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource

from apps.api.config import Settings

logger = logging.getLogger(__name__)

_METER_NAME: Final[str] = "auditpilot.api"
_EXPORT_INTERVAL_MS: Final[int] = 60_000  # once a minute is plenty for our volume

_provider: MeterProvider | None = None
_meter: metrics.Meter | None = None
_chat_request_counter: metrics.Counter | None = None
_job_processed_counter: metrics.Counter | None = None
_llm_token_counter: metrics.Counter | None = None
_init_lock = threading.Lock()


def _parse_otlp_headers(raw: str | None) -> dict[str, str]:
    """Parse ``k1=v1,k2=v2`` (URL-encoded) into a header dict.

    Grafana Cloud's OTLP console copy already contains
    ``Authorization=Basic%20<b64>`` — unquote so the OTLP exporter can
    send the header verbatim.
    """

    if not raw:
        return {}
    headers: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key, _, value = item.partition("=")
        headers[key.strip()] = unquote(value.strip())
    return headers


def init_metrics(settings: Settings) -> bool:
    """Configure the OTel meter provider + AuditPilot meters.

    Returns ``True`` on success, ``False`` if the exporter was skipped
    (missing endpoint or init failure). Idempotent — repeat calls are a
    no-op so importing this module from both FastAPI lifespan and a test
    fixture is safe.
    """

    global _provider, _meter
    global _chat_request_counter, _job_processed_counter, _llm_token_counter

    with _init_lock:
        if _provider is not None:
            return True

        endpoint = settings.otlp_endpoint
        if not endpoint:
            logger.info("metrics.disabled reason=no-otlp-endpoint")
            return False

        # OTel's OTLP/HTTP exporter appends ``/v1/metrics`` itself, so
        # strip any trailing slash for tidy URLs.
        endpoint = endpoint.rstrip("/") + "/v1/metrics"
        headers = _parse_otlp_headers(settings.otlp_headers)

        try:
            exporter = OTLPMetricExporter(endpoint=endpoint, headers=headers)
            reader = PeriodicExportingMetricReader(
                exporter,
                export_interval_millis=_EXPORT_INTERVAL_MS,
            )
            resource = Resource.create(
                {
                    SERVICE_NAME: "auditpilot-api",
                    "service.version": "0.1.0",
                    "deployment.environment": settings.environment,
                    "git_sha": settings.git_sha,
                }
            )
            _provider = MeterProvider(
                resource=resource,
                metric_readers=[reader],
            )
            metrics.set_meter_provider(_provider)
            _meter = metrics.get_meter(_METER_NAME)

            _chat_request_counter = _meter.create_counter(
                name="auditpilot.chat.requests",
                description="Count of POST /chat invocations.",
                unit="1",
            )
            _job_processed_counter = _meter.create_counter(
                name="auditpilot.jobs.processed",
                description="Count of jobs dispatched through the JobQueue worker.",
                unit="1",
            )
            _llm_token_counter = _meter.create_counter(
                name="auditpilot.llm.tokens",
                description="LLM tokens consumed, keyed by model and kind (prompt/completion).",
                unit="token",
            )
        except Exception:  # noqa: BLE001 — metrics must never block startup
            logger.exception("metrics.init_failed endpoint=%s", endpoint)
            _provider = None
            _meter = None
            _chat_request_counter = None
            _job_processed_counter = None
            _llm_token_counter = None
            return False

        logger.info(
            "metrics.enabled endpoint=%s service=auditpilot-api environment=%s",
            endpoint,
            settings.environment,
        )
        return True


def shutdown_metrics(timeout_millis: int = 2_000) -> None:
    global _provider, _meter
    global _chat_request_counter, _job_processed_counter, _llm_token_counter

    with _init_lock:
        if _provider is None:
            return
        try:
            _provider.shutdown(timeout_millis=timeout_millis)
        except Exception:  # noqa: BLE001 — best-effort teardown
            logger.exception("metrics.shutdown_failed")
        _provider = None
        _meter = None
        _chat_request_counter = None
        _job_processed_counter = None
        _llm_token_counter = None


# ─── Public helpers ───────────────────────────────────────────────────────────


def record_chat_request(*, intent: str | None = None, outcome: str = "started") -> None:
    if _chat_request_counter is None:
        return
    attributes: dict[str, str] = {"outcome": outcome}
    if intent:
        attributes["intent"] = intent
    _chat_request_counter.add(1, attributes=attributes)


def record_job_processed(*, job_type: str, status: str) -> None:
    if _job_processed_counter is None:
        return
    _job_processed_counter.add(1, attributes={"job_type": job_type, "status": status})


def record_llm_tokens(*, model: str, kind: str, count: int) -> None:
    if _llm_token_counter is None or count <= 0:
        return
    _llm_token_counter.add(count, attributes={"model": model, "kind": kind})


def is_enabled() -> bool:
    """Return True if the exporter is wired up (used by tests to skip cleanly)."""

    return _provider is not None
