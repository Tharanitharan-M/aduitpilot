"""Long-lived async worker for the JobQueue (ADR-0010 Worker placement).

Two coroutines, both intended to be spawned via ``asyncio.create_task`` in
the FastAPI lifespan (chunk 2.11):

* :func:`run_worker` — claim-process-ack loop. One running task processes
  at most one job at a time; scale by spawning more ``run_worker`` tasks
  with different ``consumer`` names.
* :func:`reclaim_stale_messages` — periodic sweep that takes over messages
  abandoned by crashed workers via ``XPENDING`` + ``XCLAIM``.

Both coroutines honour ``CancelledError`` cleanly so ``worker.cancel()``
in the lifespan ``finally`` block shuts the background tasks down without
dropping an in-flight job.

See ADR-0010 "Worker placement" for the decision to keep the worker in
the same Cloud Run service as the API for v1.0.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping

from apps.api.jobs.exceptions import FatalError, RetryableError
from apps.api.jobs.queue import JobQueue
from apps.api.jobs.schemas import JobMessage, JobType

logger = logging.getLogger(__name__)

JobDispatcher = Callable[[JobMessage], Awaitable[None]]
HandlerMap = Mapping[JobType, JobDispatcher]

DEFAULT_RECLAIM_INTERVAL_SECONDS = 30
DEFAULT_RECLAIM_IDLE_THRESHOLD_MS = 60_000
DEFAULT_WORKER_POLL_BLOCK_MS = 5_000


def make_dispatcher(handlers: HandlerMap) -> JobDispatcher:
    """Wrap a ``{JobType: handler}`` map into a single dispatcher.

    Unknown types become :class:`FatalError` so the message heads straight
    to the DLQ (not the kind of failure a retry would fix).
    """

    async def _dispatch(message: JobMessage) -> None:
        handler = handlers.get(message.type)
        if handler is None:
            raise FatalError(f"no handler registered for job type {message.type!r}")
        await handler(message)

    return _dispatch


async def run_worker(
    queue: JobQueue,
    dispatcher: JobDispatcher,
    *,
    consumer: str | None = None,
    poll_block_ms: int = DEFAULT_WORKER_POLL_BLOCK_MS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Claim → process → ack loop. Runs until cancelled or ``stop_event`` is set.

    The loop uses ``queue.process_once`` so all retry / DLQ logic lives in
    one place. A cancellation flushes cleanly: the currently-in-flight job
    finishes (or is routed to DLQ by ``retry_or_dlq``), then the loop exits.
    """

    consumer_name = consumer or f"worker-{os.getpid()}-{id(queue):x}"
    logger.info("worker.start consumer=%s stream=%s", consumer_name, queue.stream)
    await queue.ensure_group()

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info("worker.stop_event_set consumer=%s", consumer_name)
                break
            try:
                await queue.process_once(
                    consumer_name, dispatcher, block_ms=poll_block_ms
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — loop must not die on poll errors
                logger.exception(
                    "worker.poll_error consumer=%s; sleeping 1s before retry",
                    consumer_name,
                )
                await asyncio.sleep(1.0)
            # Yield control so the event loop can service HTTP requests /
            # other workers even under a hot stream.
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        logger.info("worker.cancelled consumer=%s", consumer_name)
        raise
    finally:
        logger.info("worker.exit consumer=%s", consumer_name)


async def reclaim_stale_messages(
    queue: JobQueue,
    dispatcher: JobDispatcher,
    *,
    consumer: str | None = None,
    interval_seconds: float = DEFAULT_RECLAIM_INTERVAL_SECONDS,
    idle_threshold_ms: int = DEFAULT_RECLAIM_IDLE_THRESHOLD_MS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Periodic reclaim + replay loop (ADR-0010 Implementation sketch).

    Polls ``XPENDING`` every ``interval_seconds``. Any message idle for
    longer than ``idle_threshold_ms`` is claimed by this consumer via
    ``XCLAIM`` and immediately handed to the dispatcher — same retry /
    DLQ rules as a fresh delivery.
    """

    consumer_name = consumer or f"reclaim-{os.getpid()}-{id(queue):x}"
    logger.info(
        "reclaim.start consumer=%s interval=%.1fs idle_threshold=%dms",
        consumer_name,
        interval_seconds,
        idle_threshold_ms,
    )
    await queue.ensure_group()

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                await _reclaim_once(
                    queue,
                    dispatcher,
                    consumer_name=consumer_name,
                    idle_threshold_ms=idle_threshold_ms,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — loop must not die on sweep errors
                logger.exception(
                    "reclaim.sweep_error consumer=%s", consumer_name
                )
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                raise
    except asyncio.CancelledError:
        logger.info("reclaim.cancelled consumer=%s", consumer_name)
        raise
    finally:
        logger.info("reclaim.exit consumer=%s", consumer_name)


async def _reclaim_once(
    queue: JobQueue,
    dispatcher: JobDispatcher,
    *,
    consumer_name: str,
    idle_threshold_ms: int,
) -> int:
    """One reclaim sweep — returns the number of messages reclaimed."""

    stale = await queue.list_stale(idle_ms=idle_threshold_ms, count=100)
    if not stale:
        return 0

    stale_ids = [entry["message_id"] for entry in stale]
    reclaimed = await queue.reclaim(
        consumer_name, stale_ids, min_idle_time=idle_threshold_ms
    )
    if not reclaimed:
        return 0

    logger.info(
        "reclaim.took_over consumer=%s count=%d ids=%s",
        consumer_name,
        len(reclaimed),
        [mid for mid, _ in reclaimed],
    )
    for message_id, job in reclaimed:
        try:
            await dispatcher(job)
            await queue.ack(message_id)
        except (RetryableError, FatalError) as exc:
            await queue.retry_or_dlq(message_id, job, exc)
        except Exception as exc:  # noqa: BLE001 — treat unknown as retryable
            await queue.retry_or_dlq(message_id, job, RetryableError(str(exc)))
    return len(reclaimed)
