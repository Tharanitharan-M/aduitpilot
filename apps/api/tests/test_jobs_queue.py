"""End-to-end JobQueue contract against fakeredis (chunk 2.10).

Exit-criteria proof: enqueue → claim → handler → ack happy path, plus
idempotency dedup, retry-then-DLQ, and fatal-error → DLQ.

These tests do not require a live Redis. For real-Upstash verification
see :mod:`test_jobs_queue_upstash` (marked ``integration``).
"""

from __future__ import annotations

import fakeredis
import pytest

from apps.api.jobs.exceptions import BudgetExceededError, FatalError, RetryableError
from apps.api.jobs.queue import JobQueue
from apps.api.jobs.schemas import JobMessage, JobType

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


@pytest.fixture
async def queue(redis):
    q = JobQueue(
        redis,
        stream="auditpilot:test:jobs",
        group="auditpilot-test-workers",
        dlq_stream="auditpilot:test:jobs:dlq",
    )
    await q.ensure_group()
    return q


def _sample_job(**overrides) -> JobMessage:
    defaults = dict(
        type=JobType.DRIFT_SCAN,
        user_id="user_42",
        idempotency_key="idem-" + overrides.get("suffix", "a"),
        payload={"hello": "world"},
    )
    defaults.pop("suffix", None)
    defaults.update(overrides)
    return JobMessage(**defaults)


async def test_enqueue_claim_ack_happy_path(queue: JobQueue) -> None:
    job = _sample_job()

    seen: list[JobMessage] = []

    async def handler(message: JobMessage) -> None:
        seen.append(message)

    result = await queue.enqueue(job)
    assert result.deduplicated is False
    assert result.message_id  # non-empty Redis message id

    processed = await queue.process_once("worker-1", handler, block_ms=50)
    assert processed == 1
    assert len(seen) == 1
    assert seen[0].payload == {"hello": "world"}

    # Stream drained — next poll is empty.
    assert await queue.process_once("worker-1", handler, block_ms=50) == 0


async def test_enqueue_dedupes_on_idempotency_key(queue: JobQueue) -> None:
    job = _sample_job()

    first = await queue.enqueue(job)
    second = await queue.enqueue(job)

    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.message_id.startswith("dedup:")

    claimed = await queue.claim_next("worker-1", count=10, block_ms=50)
    assert len(claimed) == 1, "deduplicated enqueue must not add a second message"


async def test_retryable_error_re_enqueues_with_attempt_incremented(
    redis, queue: JobQueue
) -> None:
    job = _sample_job()
    await queue.enqueue(job)

    attempts: list[int] = []

    async def handler(message: JobMessage) -> None:
        attempts.append(message.attempt)
        raise RetryableError("429 rate-limited")

    # Attempt 1 → retry.
    await queue.process_once("worker-1", handler, block_ms=50)
    # Attempt 2 → retry.
    await queue.process_once("worker-1", handler, block_ms=50)
    # Attempt 3 → DLQ (next_attempt would be 4, over max_attempts=3).
    await queue.process_once("worker-1", handler, block_ms=50)

    assert attempts == [1, 2, 3]

    dlq_entries = await redis.xrange(queue.dlq_stream)
    assert len(dlq_entries) == 1
    _, fields = dlq_entries[0]
    assert fields["dlq_reason"].startswith("max-attempts-exceeded")
    assert fields["idempotency_key"] == job.idempotency_key


async def test_fatal_error_goes_straight_to_dlq(redis, queue: JobQueue) -> None:
    job = _sample_job()
    await queue.enqueue(job)

    async def handler(message: JobMessage) -> None:
        raise FatalError("400 Bad Request")

    await queue.process_once("worker-1", handler, block_ms=50)

    dlq_entries = await redis.xrange(queue.dlq_stream)
    assert len(dlq_entries) == 1
    _, fields = dlq_entries[0]
    assert "FatalError" in fields["dlq_reason"]
    assert fields["final_attempt"] == "1"

    assert await queue.claim_next("worker-2", count=1, block_ms=50) == []


async def test_budget_exceeded_is_fatal(redis, queue: JobQueue) -> None:
    job = _sample_job()
    await queue.enqueue(job)

    async def handler(message: JobMessage) -> None:
        raise BudgetExceededError("per-session $0.50 cap hit")

    await queue.process_once("worker-1", handler, block_ms=50)

    dlq_entries = await redis.xrange(queue.dlq_stream)
    assert len(dlq_entries) == 1
    _, fields = dlq_entries[0]
    assert "BudgetExceededError" in fields["dlq_reason"]


async def test_unknown_exception_is_treated_as_retryable(
    redis, queue: JobQueue
) -> None:
    job = _sample_job()
    await queue.enqueue(job)

    call_count = 0

    async def handler(message: JobMessage) -> None:
        nonlocal call_count
        call_count += 1
        raise ValueError("unexpected")

    # Three attempts (1, 2, 3), then DLQ on the fourth poll.
    for _ in range(3):
        await queue.process_once("worker-1", handler, block_ms=50)

    assert call_count == 3
    dlq_entries = await redis.xrange(queue.dlq_stream)
    assert len(dlq_entries) == 1


async def test_ensure_group_is_idempotent(redis, queue: JobQueue) -> None:
    # Second call must not raise BUSYGROUP.
    await queue.ensure_group()
    await queue.ensure_group()


async def test_claim_next_tolerates_malformed_message(redis, queue: JobQueue) -> None:
    # Push a garbage message directly (bypasses JobMessage schema).
    bad_id = await redis.xadd(queue.stream, {"broken": "true"})

    claimed = await queue.claim_next("worker-1", count=1, block_ms=50)
    assert claimed == []  # bad message routed to DLQ, not returned

    dlq_entries = await redis.xrange(queue.dlq_stream)
    assert len(dlq_entries) == 1
    _, fields = dlq_entries[0]
    assert fields["dlq_reason"] == "malformed-message"
    assert fields["original_message_id"] == bad_id


async def test_list_stale_and_reclaim_round_trip(queue: JobQueue) -> None:
    import asyncio

    job = _sample_job()
    await queue.enqueue(job)
    claimed = await queue.claim_next("worker-1", count=1, block_ms=50)
    assert len(claimed) == 1

    # fakeredis treats ``idle=0`` as "strictly greater than 0" (real Redis and
    # Upstash REST both include zero-idle messages). Nudge the message past
    # 1ms so the filter returns it regardless of which backend is in play.
    await asyncio.sleep(0.01)
    stale = await queue.list_stale(idle_ms=1)
    assert len(stale) == 1
    assert stale[0]["consumer"] == "worker-1"

    ids = [entry["message_id"] for entry in stale]
    reclaimed = await queue.reclaim("worker-2", ids, min_idle_time=0)
    assert len(reclaimed) == 1
    _mid, reclaimed_job = reclaimed[0]
    assert reclaimed_job.user_id == "user_42"
