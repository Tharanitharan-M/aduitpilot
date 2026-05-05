"""Redis-Streams-backed job queue.

This is the thin abstraction that ``apps/api/main.py`` lifespan spawns a
worker against in chunk 2.11. Per ADR-0010:

- **One stream, one group.** ``auditpilot:jobs`` / ``auditpilot-workers``.
- **Idempotency keys.** ``auditpilot:idempotency:<key>`` with 24h TTL;
  second enqueue with the same key returns ``deduplicated=True``.
- **Retries.** Handlers raise :class:`RetryableError` to trigger a retry
  (1 → 2 → 3 attempts, 5s and 30s backoff). Fourth failure moves to
  ``auditpilot:jobs:dlq``. :class:`FatalError` → DLQ immediately.
- **Reclaim.** Stale pending messages (idle > 60s) are reclaimed by the
  worker via ``XPENDING`` + ``XCLAIM`` in chunk 2.11; this module just
  exposes the primitives.

Design decision: the queue never calls ``time.sleep`` or ``asyncio.sleep``
for retry backoff. Instead, the handler re-enqueues the message with an
incremented ``attempt`` counter and the worker processes it on the next
XREADGROUP pass; the delay is the *poll interval*, not a literal sleep.
This keeps tests fast and avoids blocking the consumer loop.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

from apps.api.jobs.client import RedisLike
from apps.api.jobs.exceptions import (
    BudgetExceededError,
    FatalError,
    JobError,
    RetryableError,
)
from apps.api.jobs.schemas import JobMessage, JobResult, JobType

logger = logging.getLogger(__name__)

DEFAULT_STREAM: Final[str] = "auditpilot:jobs"
DEFAULT_GROUP: Final[str] = "auditpilot-workers"
DEFAULT_DLQ_STREAM: Final[str] = "auditpilot:jobs:dlq"
DEFAULT_IDEMPOTENCY_PREFIX: Final[str] = "auditpilot:idempotency"
DEFAULT_IDEMPOTENCY_TTL_SECONDS: Final[int] = 86_400  # 24h
DEFAULT_MAX_ATTEMPTS: Final[int] = 3
# Retry delays are tracked for observability; see Design decision above.
DEFAULT_RETRY_DELAYS_SECONDS: Final[tuple[int, ...]] = (0, 5, 30)


JobHandler = Callable[[JobMessage], Awaitable[None]]


class JobQueue:
    """Thin wrapper over Redis Streams implementing the ADR-0010 contract."""

    def __init__(
        self,
        redis: RedisLike,
        *,
        stream: str = DEFAULT_STREAM,
        group: str = DEFAULT_GROUP,
        dlq_stream: str = DEFAULT_DLQ_STREAM,
        idempotency_prefix: str = DEFAULT_IDEMPOTENCY_PREFIX,
        idempotency_ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._group = group
        self._dlq_stream = dlq_stream
        self._idempotency_prefix = idempotency_prefix
        self._idempotency_ttl = idempotency_ttl_seconds
        self._max_attempts = max_attempts
        self._group_ensured = False

    @property
    def stream(self) -> str:
        return self._stream

    @property
    def group(self) -> str:
        return self._group

    @property
    def dlq_stream(self) -> str:
        return self._dlq_stream

    # ─── Setup ────────────────────────────────────────────────────────────────
    async def ensure_group(self) -> None:
        """Create the consumer group if it does not exist.

        Idempotent — uses ``MKSTREAM`` and swallows ``BUSYGROUP`` so callers
        can invoke this on every worker start without guarding it.
        """

        if self._group_ensured:
            return
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
        except Exception as exc:  # noqa: BLE001 — redis-py raises varied types here
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ensured = True

    # ─── Producer side ────────────────────────────────────────────────────────
    async def enqueue(self, message: JobMessage) -> JobResult:
        """Add a job to the stream, honouring the idempotency key.

        Returns the Redis message ID (new or previously-stored) and a
        ``deduplicated`` flag so callers can surface the right status.
        """

        key = self._idempotency_key(message.idempotency_key)
        existing_mid = await self._redis.exists(key)
        if existing_mid:
            logger.info(
                "job.dedup type=%s idempotency_key=%s",
                message.type,
                message.idempotency_key,
            )
            return JobResult(message_id=f"dedup:{message.idempotency_key}", deduplicated=True)

        message_id = await self._redis.xadd(self._stream, _serialise(message))
        await self._redis.setex(key, self._idempotency_ttl, message_id)
        logger.info(
            "job.enqueue type=%s message_id=%s idempotency_key=%s",
            message.type,
            message_id,
            message.idempotency_key,
        )
        return JobResult(message_id=message_id, deduplicated=False)

    # ─── Consumer side ────────────────────────────────────────────────────────
    async def claim_next(
        self, consumer: str, *, count: int = 1, block_ms: int = 5_000
    ) -> list[tuple[str, JobMessage]]:
        """Claim up to ``count`` new messages for ``consumer``.

        Returns ``[(message_id, JobMessage), ...]``. Empty list when the
        block timeout elapses with no messages.
        """

        await self.ensure_group()
        raw = await self._redis.xreadgroup(
            self._group,
            consumer,
            {self._stream: ">"},
            count=count,
            block=block_ms,
        )
        claimed: list[tuple[str, JobMessage]] = []
        if not raw:
            return claimed
        for _stream_name, entries in raw:
            for message_id, fields in entries:
                try:
                    job = _deserialise(fields)
                except Exception:
                    logger.exception(
                        "job.decode_failed message_id=%s fields=%r", message_id, fields
                    )
                    await self._move_to_dlq(
                        message_id, fields, "malformed-message", attempt=1
                    )
                    await self._redis.xack(self._stream, self._group, message_id)
                    continue
                claimed.append((message_id, job))
        return claimed

    async def ack(self, message_id: str) -> None:
        """Ack a successfully-processed message."""

        await self._redis.xack(self._stream, self._group, message_id)
        logger.info("job.ack message_id=%s", message_id)

    async def retry_or_dlq(
        self, message_id: str, message: JobMessage, exc: BaseException
    ) -> None:
        """Handle a failed job per the retry policy (ADR-0010 Retry policy)."""

        fatal = isinstance(exc, (FatalError, BudgetExceededError))
        if fatal:
            await self._move_to_dlq(
                message_id,
                _serialise(message),
                reason=f"{type(exc).__name__}: {exc}",
                attempt=message.attempt,
            )
            await self._redis.xack(self._stream, self._group, message_id)
            return

        next_attempt = message.attempt + 1
        if next_attempt > self._max_attempts:
            await self._move_to_dlq(
                message_id,
                _serialise(message),
                reason=f"max-attempts-exceeded: {type(exc).__name__}: {exc}",
                attempt=message.attempt,
            )
            await self._redis.xack(self._stream, self._group, message_id)
            return

        retry_message = message.model_copy(update={"attempt": next_attempt})
        await self._redis.xadd(self._stream, _serialise(retry_message))
        await self._redis.xack(self._stream, self._group, message_id)
        logger.info(
            "job.retry message_id=%s next_attempt=%d reason=%s",
            message_id,
            next_attempt,
            type(exc).__name__,
        )

    async def process_once(
        self, consumer: str, handler: JobHandler, *, block_ms: int = 5_000
    ) -> int:
        """Claim and dispatch at most one job; return the number processed.

        Designed as the unit the worker loop spins on. Returns 0 on an
        empty poll, 1 on a successful dispatch (success, retry, or DLQ).
        """

        claimed = await self.claim_next(consumer, count=1, block_ms=block_ms)
        if not claimed:
            return 0
        message_id, job = claimed[0]
        try:
            await handler(job)
        except JobError as exc:
            await self.retry_or_dlq(message_id, job, exc)
            return 1
        except Exception as exc:  # noqa: BLE001 — unknown → retryable
            await self.retry_or_dlq(message_id, job, RetryableError(str(exc)))
            return 1
        await self.ack(message_id)
        return 1

    # ─── Reclaim (Chunk 2.11 wires the loop) ──────────────────────────────────
    async def list_stale(
        self, idle_ms: int = 60_000, count: int = 100
    ) -> list[dict[str, object]]:
        """Return pending messages idle for ``idle_ms``+ (reclaim candidates)."""

        await self.ensure_group()
        return await self._redis.xpending_range(
            self._stream,
            self._group,
            min="-",
            max="+",
            count=count,
            idle=idle_ms,
        )

    async def reclaim(
        self, consumer: str, message_ids: Sequence[str], *, min_idle_time: int = 60_000
    ) -> list[tuple[str, JobMessage]]:
        """Atomically reassign ``message_ids`` to ``consumer`` via ``XCLAIM``."""

        if not message_ids:
            return []
        raw = await self._redis.xclaim(
            self._stream, self._group, consumer, min_idle_time, list(message_ids)
        )
        reclaimed: list[tuple[str, JobMessage]] = []
        for mid, fields in raw:
            try:
                reclaimed.append((mid, _deserialise(fields)))
            except Exception:
                logger.exception("job.reclaim.decode_failed message_id=%s", mid)
        return reclaimed

    # ─── Internals ────────────────────────────────────────────────────────────
    def _idempotency_key(self, key: str) -> str:
        return f"{self._idempotency_prefix}:{key}"

    async def _move_to_dlq(
        self, message_id: str, fields: dict[str, object], reason: str, attempt: int
    ) -> None:
        dlq_fields = {
            **_stringify(fields),
            "dlq_reason": reason,
            "original_message_id": str(message_id),
            "final_attempt": str(attempt),
        }
        await self._redis.xadd(self._dlq_stream, dlq_fields)
        logger.warning(
            "job.dlq original_message_id=%s reason=%s attempt=%d",
            message_id,
            reason,
            attempt,
        )


# ─── Wire-format helpers ─────────────────────────────────────────────────────


def _serialise(message: JobMessage) -> dict[str, str]:
    """Convert a JobMessage into the flat dict XADD expects.

    ``payload`` is JSON-encoded so nested dicts survive the round-trip
    through Redis (which only understands flat field lists).
    """

    return {
        "type": (
            message.type.value if isinstance(message.type, JobType) else str(message.type)
        ),
        "user_id": message.user_id,
        "idempotency_key": message.idempotency_key,
        "attempt": str(message.attempt),
        "payload": json.dumps(message.payload, sort_keys=True, separators=(",", ":")),
    }


def _deserialise(fields: dict[str, object]) -> JobMessage:
    decoded = _stringify(fields)
    payload_raw = decoded.get("payload", "{}")
    payload = json.loads(payload_raw) if payload_raw else {}
    return JobMessage(
        type=JobType(decoded["type"]),
        user_id=decoded["user_id"],
        idempotency_key=decoded["idempotency_key"],
        attempt=int(decoded.get("attempt", "1")),
        payload=payload,
    )


def _stringify(fields: dict[str, object]) -> dict[str, str]:
    """Normalise field values to ``str`` whether they arrived as bytes or not.

    redis-py without ``decode_responses=True`` returns ``bytes``; fakeredis
    and Upstash REST return ``str``. Handle both so callers don't care.
    """

    out: dict[str, str] = {}
    for k, v in fields.items():
        key = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
        if isinstance(v, (bytes, bytearray)):
            out[key] = v.decode()
        else:
            out[key] = str(v)
    return out
