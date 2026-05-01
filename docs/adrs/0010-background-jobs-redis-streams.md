# ADR-0010: Background Job Queue via Redis Streams on Upstash

**Date:** 2026-05-01
**Status:** Accepted
**Deciders:** AuditPilot maintainers
**Refs:** SRS FR-029 through FR-035, FR-036 through FR-040; system-design.md §11; PLAN.md Sprint 7, Sprint 9; ADR-0008

---

## Context and Problem Statement

AuditPilot has multiple operations that exceed the lifetime of a single HTTP request:

- **Questionnaire upload + fill.** Parsing a 128-question SIG-Lite XLSX, clustering by domain, retrieving evidence per cluster, drafting an answer per cell, and uploading the filled file to R2 takes 60–180 seconds wall-clock.
- **Drift watcher cron.** Diffing the current evidence snapshot against the previous snapshot for every monitored control, generating Pending Action cards, and writing drift events takes 30–120 seconds per user.
- **Adversarial mock readiness challenge.** The auditor agent runs up to 30 turns over 10–60 seconds in a separate Cloud Run service, and the orchestrator must collect findings asynchronously.
- **Policy DOCX generation.** Markdown-to-DOCX conversion + R2 upload takes 5–10 seconds; user-facing latency budget says return immediately.

Cloud Run, our backend host, can terminate idle containers without warning when traffic drops to zero (scale-to-zero is exactly the property that makes the free tier work). In-process async with `asyncio.create_task(...)` will lose work whenever the container is reaped between the user's POST and the background coroutine's completion.

Even at AuditPilot's intended portfolio scale (5 users), the failure mode is real: a hiring manager uploads a sample SIG-Lite, the upload returns 202 Accepted, the container is reaped 30 seconds later, and the questionnaire run is stuck in `parsing` status forever.

The system needs a background job mechanism that:

1. **Persists job state outside the FastAPI process.** A reaped container must not destroy in-flight work.
2. **Supports retries with backoff.** Transient failures (LLM 429s, network blips on the GitHub MCP, R2 5xxs) must not fail the whole job.
3. **Has a dead-letter queue.** Jobs that fail after N retries must be parked for inspection rather than retried forever.
4. **Adds zero new vendors.** AuditPilot already runs at $0/month across six providers (ADR-0008). Adding a seventh adds operational surface, env var management, and a sixth-provider coupling.
5. **Is interview-defensible.** The choice has to read as "appropriate engineering" rather than "cargo-cult Kafka" or "naive in-process try/except."

---

## Decision

**Use Redis Streams via Upstash for background job queueing. Build a reusable `JobQueue` abstraction in `apps/api/jobs/` that wraps Redis Streams primitives with idempotency keys, exponential-backoff retries, and a dead-letter stream.**

The worker runs as a long-lived async task inside the same FastAPI Cloud Run service (`apps/api`). On container start, the worker thread spawns and pulls jobs from the `auditpilot:jobs` stream via `XREADGROUP`. Each job has an idempotency key so duplicate enqueues are no-ops. After three failed attempts a job moves to `auditpilot:jobs:dlq` for inspection.

**No new infrastructure is added.** Upstash Redis is already in the stack for rate limits and JWT verification cache. Redis Streams is a built-in Redis 5+ feature; Upstash supports the full Streams API (`XADD`, `XREADGROUP`, `XACK`, `XPENDING`, `XCLAIM`).

---

## Rationale

### Why a real queue protocol, not a database table

A Postgres `jobs` table with `SELECT ... FOR UPDATE SKIP LOCKED` is the simplest possible alternative. ~50 lines of code. Survives container restarts because state is in Postgres. For 5 users, completely sufficient.

The reason against: every senior reviewer will ask "why didn't you use a queue?" If the answer is "I didn't need one at this scale," that is correct but lands as defensive. If the answer is "I used Redis Streams because the consumer-group + ACK + dead-letter semantics are real queue primitives, and I get them on infrastructure I already pay zero for," that lands as deliberate engineering.

The cost of choosing Redis Streams over a Postgres table is approximately one extra day of build time (the `JobQueue` abstraction itself). The cost of choosing Postgres is the answer "no, I did not use a queue" in every conversation about the project.

### Why not Kafka

Kafka at 5 users is over-engineering. The honest answer in a conversation about this choice would have to be "I added Kafka because it is a resume keyword." That is the wrong reason to add infrastructure to a portfolio project. A senior reviewer would notice immediately.

If AuditPilot ever needs Kafka-class throughput (1,000+ jobs/sec, multiple regions, log-replay semantics), the migration path is documented in §6 below.

### Why not Cloud Tasks

Cloud Tasks is a strong production answer. It is GCP-native and integrates cleanly with Cloud Run. The reasons against:

1. **GCP coupling.** A fork that wants to run on AWS or self-host on Kubernetes has to rewrite the queue layer.
2. **Setup time.** Cloud Tasks needs IAM roles, queue config, and a separate worker Cloud Run service. Half a day of setup.
3. **No portfolio differentiation.** "I used Cloud Tasks" is fine but not interesting; "I built a job queue on Redis Streams with consumer groups, idempotency, exponential backoff, and a dead-letter stream" is interesting.

### Why not Inngest

Inngest is the modern "step-function as code" option. Excellent developer experience. Generous free tier. The reasons against:

1. **Sixth managed service.** AuditPilot already has six providers (Vercel, Cloud Run, Neon, Supabase, R2, Upstash). Adding Inngest is a seventh. Each new vendor is a new failure mode.
2. **Vendor lock-in.** Inngest's step-function model is opinionated. Migrating away later means rewriting business logic, not just swapping a transport.
3. **No artifact value.** Inngest hides queue mechanics. A reviewer reading the code does not see consumer groups, ACKs, or DLQs — they see decorators that hide the implementation.

### Why Redis Streams specifically (not Redis Pub/Sub, not Lists with BLPOP)

Redis has three queue-shaped primitives:

| Primitive | Persistence | Multiple consumers | ACK semantics | DLQ-friendly |
|---|---|---|---|---|
| Pub/Sub | None (fire and forget) | Yes (broadcast) | No | No |
| List + BLPOP | Yes | Round-robin | No (work is lost on consumer crash) | No |
| Streams + Consumer Groups | Yes | Yes (load-balanced) | **Yes (XACK)** | **Yes (XPENDING + XCLAIM)** |

Streams is the only one with the persistence + consumer-group + ACK semantics needed for reliable background work. It is the right primitive.

### Idempotency key design

Every enqueue accepts an `idempotency_key`. The key is the SHA-256 of `(user_id, job_type, payload_hash)`. Before adding a message to the stream, the worker checks Redis for `auditpilot:idempotency:<key>` — if present, the enqueue is a no-op. After successful processing, the key is set with a 24-hour TTL.

This handles the canonical retry case: user clicks "Run scan" twice in 200ms (network blip + manual retry). Two enqueue calls; one job processed.

### Retry policy

| Attempt | Delay before retry | Cumulative latency |
|---|---|---|
| 1 (initial) | 0 | 0 |
| 2 | 5 seconds | 5s |
| 3 | 30 seconds | 35s |
| 4 (DLQ) | n/a | parked |

After three attempts, the job moves to the dead-letter stream with the failure reason, the original payload, and the trace IDs from each attempt. The DLQ has no automatic retry; an operator manually re-enqueues from the DLQ after fixing the root cause.

The retry trigger:
- `429` from any LLM provider → retry
- `5xx` from Cloud Run, R2, GitHub, Langfuse → retry
- `400`, `401`, `403`, `404` → DLQ immediately (no retry, the job is malformed)
- `BudgetExceededError` (per ADR-0002) → DLQ immediately

### Worker placement: same Cloud Run service vs. separate

Three options were considered:

1. **Same service as `apps/api`.** Worker thread inside FastAPI process.
2. **Separate Cloud Run service `apps/worker`.** Dedicated worker, scales independently.
3. **Separate Cloud Run service with min-instances=1.** Always-on worker.

Decision: option 1 for v1.0. Fewest moving parts. Cloud Run min-instances=0 means the worker also scales to zero, which is fine — when traffic resumes, the worker starts with the container and resumes from the consumer group's position via `XPENDING`. Jobs that were in-flight when the container died are reclaimed by `XCLAIM` after their pending timeout (default 60 seconds).

Migration path to option 2 is one config change: extract `apps/api/jobs/worker.py` into `apps/worker/main.py` and redeploy to a second Cloud Run service. Documented in §6.

### Job types and stream layout

Five job types in v1:

| Job type | Producer | Worker action |
|---|---|---|
| `questionnaire.fill` | `POST /api/questionnaire/upload` | Parse XLSX → cluster → retrieve → draft → assemble → upload |
| `policy.finalize` | Approval at HumanReviewGate | Render Markdown → convert to DOCX → upload to R2 |
| `mock_audit.run` | `POST /api/mock-audit/run` | Dispatch to AdversarialAuditor over A2A v1.0, poll for findings, merge |
| `drift.scan` | Vercel Cron → `POST /api/drift/run` | Diff current evidence vs. last snapshot per control |
| `evidence.compact` | Daily cron at 02:00 UTC | Move evidence rows older than 90 days to R2 archive |

All five share one stream (`auditpilot:jobs`) and one consumer group (`auditpilot-workers`). Job type discrimination is in the message payload: `{ "type": "questionnaire.fill", "user_id": "...", "payload": {...} }`.

The decision to use one stream + one group (rather than per-type streams) is operational: fewer streams to monitor, simpler XPENDING queries, no risk of head-of-line blocking on a noisy job type because each consumer can claim work independently.

---

## Implementation sketch

```python
# apps/api/jobs/queue.py
class JobQueue:
    def __init__(self, redis: Redis, stream: str, group: str):
        self.redis = redis
        self.stream = stream
        self.group = group

    async def enqueue(self, job_type: str, payload: dict, idempotency_key: str) -> str:
        key = f"auditpilot:idempotency:{idempotency_key}"
        if await self.redis.exists(key):
            return "duplicate"
        msg_id = await self.redis.xadd(
            self.stream,
            {"type": job_type, "payload": json.dumps(payload), "idempotency_key": idempotency_key, "attempt": 1},
        )
        await self.redis.setex(key, 86400, msg_id)
        return msg_id

    async def consume(self, consumer_name: str, handler: Callable):
        while True:
            msgs = await self.redis.xreadgroup(self.group, consumer_name, {self.stream: ">"}, count=1, block=5000)
            if not msgs:
                continue
            for stream, entries in msgs:
                for msg_id, fields in entries:
                    try:
                        await handler(json.loads(fields["payload"]), fields["type"])
                        await self.redis.xack(self.stream, self.group, msg_id)
                    except RetryableError as e:
                        await self._retry_or_dlq(msg_id, fields, e)
                    except FatalError as e:
                        await self._move_to_dlq(msg_id, fields, e)
```

The worker is started in `apps/api/main.py` lifespan:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    queue = JobQueue(redis, "auditpilot:jobs", "auditpilot-workers")
    worker = asyncio.create_task(queue.consume(f"worker-{os.getpid()}", dispatch_handler))
    yield
    worker.cancel()
```

The dispatch handler routes by `type` to the right service module:

```python
async def dispatch_handler(payload: dict, job_type: str) -> None:
    handlers = {
        "questionnaire.fill": fill_questionnaire,
        "policy.finalize": finalize_policy,
        "mock_audit.run": run_mock_audit,
        "drift.scan": run_drift_scan,
        "evidence.compact": compact_evidence,
    }
    await handlers[job_type](payload)
```

Reclaim is handled by a separate periodic task that runs `XPENDING` for messages older than 60 seconds and uses `XCLAIM` to take ownership:

```python
async def reclaim_stale():
    while True:
        await asyncio.sleep(30)
        pending = await redis.xpending_range(stream, group, "-", "+", count=100, idle=60_000)
        for msg in pending:
            await redis.xclaim(stream, group, my_consumer, 60_000, [msg["message_id"]])
```

Total LOC for the queue + worker + reclaim: approximately 250 Python lines + 50 lines of tests.

---

## Consequences

### Positive

- Real queue semantics (consumer groups, ACK, DLQ) without adding a new vendor
- Survives Cloud Run container reaping; jobs reclaimed via `XPENDING` + `XCLAIM`
- Idempotent enqueue handles double-click and network retry cases
- DLQ pattern means fatal failures are inspectable, not lost
- Shared with rate limits and JWT cache on the same Upstash Redis instance
- Migration path to a separate worker service or Cloud Tasks is well-defined

### Negative

- The 10,000 commands/day Upstash free tier covers ≤ 50 jobs/day comfortably; a Show HN spike could exhaust it. Detection: Better Stack alarm on Upstash usage > 80%. Mitigation: upgrade to Upstash pay-as-you-go (~$0.20/100k commands).
- Worker thread inside `apps/api` means a hot loop in the worker can starve HTTP request handling. Mitigation: `asyncio.sleep(0)` between job dispatches; max one in-flight job per worker.
- Redis Streams is durable but not replicated across regions on the free tier; loss of the Upstash region means in-flight jobs are lost. Acceptable for v1.0.
- Reclaim logic is non-trivial; `XCLAIM` has subtle semantics around minimum idle time.

---

## Open questions deferred to implementation

1. **Per-job-type concurrency limits.** Should `mock_audit.run` be limited to one in-flight per user (the user pressing the button twice in 30 seconds)? Default: yes, enforce via Postgres advisory locks rather than Redis.
2. **Job result delivery.** Synchronous SSE on the original POST connection vs. polling vs. webhook. v1 uses SSE (the connection stays open until the job finishes or the client disconnects).
3. **Retry budget interaction with cost cap.** A retry consumes LLM tokens. Should retries draw from the same per-session budget, or get a fresh budget? Default: same budget; a budget-exceeded retry triggers an immediate DLQ.

---

## Migration paths

| Trigger | Migration |
|---|---|
| Worker hot loop blocks HTTP | Extract `apps/worker/` as separate Cloud Run service |
| > 10k commands/day on Upstash free tier | Upstash pay-as-you-go (linear cost, ~$0.20/100k commands) |
| > 1,000 jobs/sec sustained | Migrate to Cloud Tasks (provider-managed) or Kafka/Confluent (cross-region) |
| Cross-region durability needed | Cross-region Redis replication (Upstash Pro) or migrate to Cloud Tasks |
| Multi-language workers | Standardize on the JobMessage Pydantic schema; any language with a Redis client can consume |

The single layer that matters for migration is the `JobMessage` schema (`apps/api/jobs/schemas.py`). As long as it is stable, the transport layer underneath can change without rewriting handlers.

---

## Alternatives Considered

| Option | Why rejected |
|---|---|
| **Postgres `jobs` table + SKIP LOCKED** | Functionally correct at our scale. Rejected on portfolio-signal grounds: "I built a Postgres job queue" is a weaker conversation than "I built a job queue on Redis Streams with consumer groups, ACK, and DLQ." Both are honest engineering; the second is more interesting in review. |
| **Cloud Tasks** | GCP-coupling. Adds half a day of setup. No portfolio differentiation. |
| **Inngest** | Seventh managed service. Vendor lock-in. Hides queue mechanics — reviewer reading the code does not see real primitives. |
| **Kafka (Confluent Cloud)** | Over-engineering at 5 users. The honest defense for this choice does not exist; "I added Kafka because it is a resume keyword" is the wrong reason to add infrastructure. |
| **AWS SQS** | AWS coupling. We are on GCP for the backend; mixing clouds for a queue creates cross-cloud egress costs and IAM complexity. |
| **Celery + Redis broker** | Celery's API is verbose, the worker model is opinionated, and the Python-only constraint locks out future polyglot workers. Streams + a thin abstraction is cleaner. |
| **Native Redis Lists + BLPOP** | No ACK semantics; if the worker crashes mid-job, the work is lost. Streams + Consumer Groups is the right primitive. |
| **Ignore the problem** | Cloud Run will eat unhandled background work the first time a real user uploads a real questionnaire. Not a viable v1.0. |

---
