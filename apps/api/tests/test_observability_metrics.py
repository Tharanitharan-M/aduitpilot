"""Metrics exporter contract (chunk 2.14, ADR-0009).

Uses an in-memory OTel reader so tests can assert that the three custom
counters emit the right points without touching Grafana. A live-export
sanity check is left to the Step-Report manual test (ADR-0009 
Grafana verification).
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource

import apps.api.observability.metrics as metrics_module
from apps.api.observability.metrics import (
    _parse_otlp_headers,
    record_chat_request,
    record_job_processed,
    record_llm_tokens,
)


@pytest.fixture
def reader() -> Generator[InMemoryMetricReader, None, None]:
    """Swap the module's counters for ones bound to a fresh InMemory reader.

    We build a provider-local meter directly instead of going through
    ``metrics.set_meter_provider`` — OTel only allows the global provider
    to be set once per process, so repeated fixture invocations silently
    fall back to whatever provider the first test installed.
    """

    in_memory_reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({SERVICE_NAME: "auditpilot-api-test"}),
        metric_readers=[in_memory_reader],
    )
    meter = provider.get_meter("auditpilot.api.test")

    original_chat = metrics_module._chat_request_counter
    original_job = metrics_module._job_processed_counter
    original_llm = metrics_module._llm_token_counter

    metrics_module._chat_request_counter = meter.create_counter(  # type: ignore[assignment]
        "auditpilot.chat.requests", unit="1"
    )
    metrics_module._job_processed_counter = meter.create_counter(  # type: ignore[assignment]
        "auditpilot.jobs.processed", unit="1"
    )
    metrics_module._llm_token_counter = meter.create_counter(  # type: ignore[assignment]
        "auditpilot.llm.tokens", unit="token"
    )

    try:
        yield in_memory_reader
    finally:
        metrics_module._chat_request_counter = original_chat  # type: ignore[assignment]
        metrics_module._job_processed_counter = original_job  # type: ignore[assignment]
        metrics_module._llm_token_counter = original_llm  # type: ignore[assignment]
        provider.shutdown()


def _collect(reader: InMemoryMetricReader) -> dict[str, list]:
    """Flatten the reader's snapshot into ``{metric_name: [points...]}``."""

    data = reader.get_metrics_data()
    out: dict[str, list] = {}
    if not data:
        return out
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                # Sum is the only aggregation for counters.
                for point in metric.data.data_points:
                    out.setdefault(metric.name, []).append(point)
    return out


def test_parse_otlp_headers_handles_url_encoded_grafana_format() -> None:
    raw = "Authorization=Basic%20abc123,Another-Header=plain-value"
    parsed = _parse_otlp_headers(raw)
    assert parsed == {
        "Authorization": "Basic abc123",
        "Another-Header": "plain-value",
    }


def test_parse_otlp_headers_returns_empty_dict_for_none() -> None:
    assert _parse_otlp_headers(None) == {}
    assert _parse_otlp_headers("") == {}


def test_record_chat_request_emits_point_with_intent_attribute(
    reader: InMemoryMetricReader,
) -> None:
    record_chat_request(intent="run_readiness_scan", outcome="started")
    record_chat_request(intent="free_chat", outcome="started")

    snapshot = _collect(reader)
    points = snapshot["auditpilot.chat.requests"]
    assert len(points) == 2
    intents = {p.attributes.get("intent") for p in points}
    assert intents == {"run_readiness_scan", "free_chat"}
    assert all(p.attributes.get("outcome") == "started" for p in points)


def test_record_job_processed_emits_by_type_and_status(
    reader: InMemoryMetricReader,
) -> None:
    record_job_processed(job_type="drift.scan", status="succeeded")
    record_job_processed(job_type="drift.scan", status="succeeded")
    record_job_processed(job_type="drift.scan", status="failed")

    points = _collect(reader)["auditpilot.jobs.processed"]
    # Two attribute sets → two points; values reflect counts.
    by_status = {p.attributes["status"]: p.value for p in points}
    assert by_status == {"succeeded": 2, "failed": 1}


def test_record_llm_tokens_adds_counts_by_model(reader: InMemoryMetricReader) -> None:
    record_llm_tokens(model="gemini-2.5-flash-lite", kind="prompt", count=400)
    record_llm_tokens(model="gemini-2.5-flash-lite", kind="completion", count=250)
    record_llm_tokens(model="gemini-2.5-flash-lite", kind="prompt", count=0)  # ignored

    points = _collect(reader)["auditpilot.llm.tokens"]
    by_kind = {p.attributes["kind"]: p.value for p in points}
    assert by_kind == {"prompt": 400, "completion": 250}


def test_record_helpers_are_noop_when_exporter_disabled() -> None:
    # Clear globals so counters are None — mimics the $0/month dev path.
    import apps.api.observability.metrics as m

    saved_chat = m._chat_request_counter
    saved_job = m._job_processed_counter
    saved_llm = m._llm_token_counter
    m._chat_request_counter = None  # type: ignore[assignment]
    m._job_processed_counter = None  # type: ignore[assignment]
    m._llm_token_counter = None  # type: ignore[assignment]
    try:
        record_chat_request(intent="x")
        record_job_processed(job_type="drift.scan", status="succeeded")
        record_llm_tokens(model="m", kind="prompt", count=10)
    finally:
        m._chat_request_counter = saved_chat  # type: ignore[assignment]
        m._job_processed_counter = saved_job  # type: ignore[assignment]
        m._llm_token_counter = saved_llm  # type: ignore[assignment]


def test_init_metrics_skips_gracefully_when_no_endpoint() -> None:
    """Dev path: no OTLP endpoint → initialization is a no-op returning False."""

    from apps.api.config import Settings
    from apps.api.observability.metrics import init_metrics, shutdown_metrics

    fake_settings = Settings.model_construct(
        environment="development",
        database_url="postgres://x",
        redis_url="redis://localhost:6379/0",
        clerk_secret_key="sk",
        clerk_publishable_key="pk",
        gemini_api_key="g",
        langfuse_public_key="p",
        langfuse_secret_key="s",
        langfuse_host="https://cloud.langfuse.com",
        otlp_endpoint=None,
        otlp_headers=None,
        git_sha="local",
    )
    assert init_metrics(fake_settings) is False
    shutdown_metrics()
