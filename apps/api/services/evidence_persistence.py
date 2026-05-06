"""
Evidence persistence — Sprint 5 chunk 5.1 / 5.3–5.7
=====================================================
Persists in-memory ``Evidence`` rows (from the orchestrator graph's
``collect_evidence`` node) to the ``evidence`` table in Neon Postgres
and optionally backfills the ``embedding`` column using Gemini
text-embedding-004.

Design choices
--------------
* **Upsert, not insert** — ``ON CONFLICT (user_id, content_hash) DO NOTHING``
  means identical evidence (same normalized payload, same user) is never
  duplicated. Re-scans within 24 h mostly produce zero new rows.
* **Embedding on write** — we generate embeddings synchronously during
  ``persist_evidence``.  If ``GEMINI_API_KEY`` is absent (dev / test) we
  skip embedding generation and leave ``embedding = NULL``; the HNSW index
  simply won't cover those rows.
* **Batch embedding** — all rows for one collector invocation are embedded in
  a single ``asyncio.gather`` to bound wall-clock time.
* **No token in state** — the Gemini API key is passed explicitly from
  ``Settings``; it never enters LangGraph state and is therefore never
  checkpointed.

Refs: PLAN.md Sprint 5 chunks 5.1, 5.3–5.7; system-design.md §12.5;
ADR-0008 (Neon Postgres + pgvector).
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from typing import Any

import httpx
from opentelemetry import trace
from pgvector.psycopg import register_vector_async

from apps.api.state import Evidence

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


# Sprint 5 chunk 5.14 — typed pgvector adapter.
#
# Previous implementation built a string literal via
# ``f"[{','.join(str(float(v)) for v in embedding)}]"`` and bound it as
# a regular text parameter cast with ``%s::vector``. The ``float()``
# coercion guarded against malformed Gemini responses, but the wire
# encoding still went through psycopg's text path — a single ``inf`` or
# ``nan`` would land on the server as the strings ``"inf"`` / ``"nan"``
# and pgvector would reject the INSERT mid-batch.
#
# ``register_vector_async(conn)`` registers psycopg type adapters that
# serialise ``list[float]`` and ``numpy.ndarray`` directly as the
# pgvector wire type. The bind is fully typed, the SQL is parameterised
# end to end, and the explicit ``::vector`` cast disappears from the
# call sites.
#
# Registration state is per-process. ``_registered_conns`` is a WeakSet
# of connections we have already registered on (so we don't repeat the
# work on every checkout of a long-lived pool member).
# ``_adapter_disabled`` is a circuit breaker: if the first registration
# attempt fails (e.g. pgvector extension not loaded on the target
# schema, or psycopg version mismatch), the flag stays False forever
# and every subsequent call short-circuits to a NULL embedding instead
# of hammering the DB with failing registration attempts (python-
# reviewer F1) or binding a raw list[float] that has no registered
# pgvector adapter and would crash mid-INSERT (python-reviewer F2).

_registered_conns: weakref.WeakSet[Any] = weakref.WeakSet()
_adapter_disabled: bool = False


async def _ensure_vector_adapter(conn: Any) -> bool:
    """Register the pgvector psycopg adapter on this connection.

    Returns True when the adapter is registered (caller may bind
    ``list[float]`` directly); False when the adapter could not be
    registered and the caller MUST coerce embedding parameters to
    ``None`` before binding (otherwise psycopg raises
    ``ProgrammingError: can't adapt type 'list'``).

    On the first failure, ``_adapter_disabled`` flips to True and every
    subsequent call returns False without touching the DB — closes the
    retry-storm hazard from python-reviewer F1.
    """

    global _adapter_disabled
    if _adapter_disabled:
        return False
    if conn in _registered_conns:
        return True
    try:
        await register_vector_async(conn)
        _registered_conns.add(conn)
        return True
    except Exception as exc:  # noqa: BLE001
        _adapter_disabled = True
        logger.warning(
            "pgvector.adapter.register_failed type=%s — adapter disabled "
            "process-wide; embeddings will be stored as NULL until restart",
            type(exc).__name__,
        )
        return False

# Gemini text-embedding-004 endpoint. Pinned to the v1 stable surface so the
# embedding dimensionality (768) matches the vector column width set in
# migration 0005_evidence.sql. The API key is sent via the x-goog-api-key
# header so it never appears in URLs, logs, or proxy access lines.
# gemini-embedding-001 is the current stable Gemini embedding model
# (text-embedding-004 was retired). Default output is 3072 dim; we request
# 768 dim explicitly via outputDimensionality so the response fits the
# `vector(768)` column declared in migration 0005_evidence.sql.
_GEMINI_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-embedding-001:embedContent"
)
_GEMINI_EMBED_DIMS = 768


# ── Embedding generation ─────────────────────────────────────────────────────


async def _generate_embedding(
    text: str,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[float] | None:
    """Call Gemini text-embedding-004 and return the 768-dim float vector.

    Returns ``None`` on any error so callers can store ``embedding = NULL``
    and retry later. Never raises.
    """

    if not text.strip():
        return None

    try:
        payload = {
            "model": "models/gemini-embedding-001",
            "content": {"parts": [{"text": text[:8000]}]},  # cap at 8 K chars
            "outputDimensionality": _GEMINI_EMBED_DIMS,
        }
        # Use x-goog-api-key header so the key never appears in the URL,
        # which would leak into httpx exception repr and proxy access logs.
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        url = _GEMINI_EMBED_URL

        if client is not None:
            r = await client.post(url, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=15.0) as _client:
                r = await _client.post(url, json=payload, headers=headers)

        if r.status_code != 200:
            logger.warning(
                "embedding.failed status=%d body=%.120s",
                r.status_code,
                r.text,
            )
            return None

        body = r.json()
        values: list[float] = [float(v) for v in body["embedding"]["values"]]
        return values

    except Exception as exc:  # noqa: BLE001
        # Log only the exception type — never the URL or headers (key leak risk).
        logger.warning("embedding.exception type=%s", type(exc).__name__)
        return None


def _evidence_to_embed_text(evidence: Evidence) -> str:
    """Build a short text representation of an Evidence row for embedding.

    The text is what the HNSW index will recall when the MCP tool's
    ``search_evidence`` query is semantically close to the stored content.
    We include the source_type, source_uri, and a flattened version of
    the raw payload so domain terms (e.g. "branch protection required_reviews")
    are present in the vector space.
    """

    parts: list[str] = [
        f"source_type:{evidence.source_type}",
    ]
    if evidence.source_uri:
        parts.append(f"uri:{evidence.source_uri}")

    raw = evidence.raw or {}
    # Include readable key=value pairs from the raw payload (skip large blobs).
    for key, value in raw.items():
        if isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}:{value}")
        elif isinstance(value, dict):
            for k2, v2 in value.items():
                if isinstance(v2, (str, int, float, bool)):
                    parts.append(f"{key}.{k2}:{v2}")

    return " ".join(parts)


# ── Persistence ──────────────────────────────────────────────────────────────


def _require_user_id(user_id: str | None) -> str:
    """Guard: raise if user_id is falsy so RLS is never silently bypassed."""
    if not user_id:
        raise ValueError("user_id is required for RLS context — cannot be None or empty")
    return user_id


async def persist_evidence(
    evidence_list: list[Evidence],
    user_id: str,
    pool: Any,  # psycopg AsyncConnectionPool
    *,
    gemini_api_key: str | None = None,
) -> int:
    """Upsert ``evidence_list`` into the ``evidence`` table.

    Parameters
    ----------
    evidence_list:
        Evidence rows produced by the ``collect_evidence`` graph node.
        ``user_id`` is set on each row before INSERT.
    user_id:
        Clerk user_id used for the RLS context and the ``user_id`` column.
    pool:
        psycopg ``AsyncConnectionPool`` from the FastAPI lifespan.
    gemini_api_key:
        When present, embeddings are generated for each row before INSERT.
        When absent, rows are inserted with ``embedding = NULL``.

    Returns
    -------
    int
        Number of rows actually inserted (ON CONFLICT DO NOTHING silently
        skips duplicates, so this may be less than ``len(evidence_list)``).
    """

    if not evidence_list:
        return 0

    uid = _require_user_id(user_id)

    with tracer.start_as_current_span("evidence_persistence.persist") as span:
        span.set_attribute("evidence.input_count", len(evidence_list))
        span.set_attribute("user_id", uid)

        # Stamp each row with the caller's user_id.
        for ev in evidence_list:
            ev.user_id = uid

        # Generate embeddings in parallel (all rows at once).
        # return_exceptions=True so a single Gemini failure doesn't abort the
        # entire persist call — affected rows get embedding=NULL instead.
        embeddings: list[list[float] | None] = []
        if gemini_api_key:
            async with httpx.AsyncClient(timeout=15.0) as embed_client:
                raw_results = await asyncio.gather(
                    *[
                        _generate_embedding(
                            _evidence_to_embed_text(ev),
                            gemini_api_key,
                            client=embed_client,
                        )
                        for ev in evidence_list
                    ],
                    return_exceptions=True,
                )
                embeddings = [
                    None if isinstance(r, BaseException) else r
                    for r in raw_results
                ]
        else:
            embeddings = [None] * len(evidence_list)

        # Upsert rows.
        inserted = 0
        async with pool.connection() as conn:
            # Sprint 5 chunk 5.14 — register the pgvector adapter so the
            # ``embedding`` parameter binds as a typed vector via psycopg
            # rather than as a text literal cast in SQL.
            adapter_ok = await _ensure_vector_adapter(conn)
            await conn.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                (uid,),
            )
            for ev, embedding in zip(evidence_list, embeddings, strict=False):
                raw_json = json.dumps(ev.raw)

                # ``_generate_embedding`` already returns ``list[float] | None``;
                # we pass it straight through when the adapter is registered.
                # When the adapter is disabled (python-reviewer F2), bind
                # NULL instead of a raw ``list[float]`` — psycopg has no
                # type adapter for ``list`` and would raise
                # ``ProgrammingError: can't adapt type 'list'`` mid-INSERT.
                embedding_typed: list[float] | None = (
                    embedding if (adapter_ok and embedding is not None) else None
                )

                result = await conn.execute(
                    """
                    INSERT INTO evidence
                        (id, scan_run_id, user_id, source_type, source_uri,
                         raw, content_hash, embedding, collected_at, valid_until)
                    VALUES
                        (gen_random_uuid(), %s, %s, %s, %s,
                         %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (user_id, content_hash) DO NOTHING
                    """,
                    (
                        ev.scan_run_id,
                        uid,
                        ev.source_type,
                        ev.source_uri,
                        raw_json,
                        ev.content_hash,
                        embedding_typed,
                        ev.collected_at,
                        ev.valid_until,
                    ),
                )
                if result.rowcount:
                    inserted += result.rowcount

        span.set_attribute("evidence.inserted_count", inserted)
        span.set_attribute("evidence.skipped_count", len(evidence_list) - inserted)
        return inserted


# ── Control-map cache ────────────────────────────────────────────────────────


async def fetch_cached_assessments(
    content_hashes: list[str],
    user_id: str,
    pool: Any,
    *,
    prompt_version: str = "v1",
    kb_version: str = "0.2.0",
) -> dict[str, dict[str, Any]]:
    """Load control_map_cache rows for a batch of content_hashes.

    Returns ``{content_hash: {control_id: {status, confidence, ...}}}`` so
    callers can skip BM25 for any hash that already has a cached assessment.
    """

    if not content_hashes:
        return {}

    uid = _require_user_id(user_id)

    with tracer.start_as_current_span("evidence_persistence.fetch_cached_assessments") as span:
        span.set_attribute("user_id", uid)
        span.set_attribute("cache.hash_count", len(content_hashes))

        async with pool.connection() as conn:
            await conn.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                (uid,),
            )
            rows = await conn.execute(
                """
                SELECT content_hash, control_id, status, confidence,
                       nist_800_53_refs, evidence_ids, rationale
                FROM   control_map_cache
                WHERE  user_id = %s
                  AND  content_hash = ANY(%s::text[])
                  AND  prompt_version = %s
                  AND  kb_version = %s
                """,
                (uid, content_hashes, prompt_version, kb_version),
            )
            result: dict[str, dict[str, Any]] = {}
            async for row in rows:
                h, ctrl, status, conf, nist_refs, ev_ids, rationale = row
                result.setdefault(h, {})[ctrl] = {
                    "status": status,
                    "confidence": conf,
                    "nist_800_53_refs": nist_refs or [],
                    "evidence_ids": ev_ids or [],
                    "rationale": rationale,
                }
            span.set_attribute("cache.hit_count", len(result))
            return result


async def write_cached_assessments(
    assessments: dict[str, Any],  # tsc_id → ControlAssessment
    content_hashes: list[str],
    user_id: str,
    pool: Any,
    *,
    prompt_version: str = "v1",
    kb_version: str = "0.2.0",
) -> None:
    """Upsert ControlAssessment results into control_map_cache.

    Associates each tsc_id assessment with every content_hash that contributed
    to it, so a re-scan with the same evidence hash gets a cache hit.
    """

    if not assessments or not content_hashes:
        return

    uid = _require_user_id(user_id)

    with tracer.start_as_current_span("evidence_persistence.write_cached_assessments") as span:
        span.set_attribute("user_id", uid)
        span.set_attribute("cache.assessment_count", len(assessments))
        span.set_attribute("cache.hash_count", len(content_hashes))

        # Sprint 5 chunk 5.17 — single executemany pipelines all
        # (tsc_id × content_hash) rows in one DB round-trip instead of
        # the prior nested-loop ``conn.execute`` per pair. For 10 TSC
        # clauses × 50 content_hashes this collapsed 500 round-trips
        # down to one.
        rows: list[tuple[Any, ...]] = [
            (
                uid,
                h,
                tsc_id,
                prompt_version,
                kb_version,
                assessment.status,
                assessment.confidence,
                assessment.nist_800_53_refs,
                assessment.evidence_ids,
                assessment.rationale,
            )
            for tsc_id, assessment in assessments.items()
            for h in content_hashes
        ]

        async with pool.connection() as conn:
            await conn.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                (uid,),
            )
            async with conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO control_map_cache
                        (user_id, content_hash, control_id, prompt_version, kb_version,
                         status, confidence, nist_800_53_refs, evidence_ids, rationale)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s)
                    ON CONFLICT (user_id, content_hash, control_id, prompt_version, kb_version)
                    DO UPDATE SET
                        status           = EXCLUDED.status,
                        confidence       = EXCLUDED.confidence,
                        nist_800_53_refs = EXCLUDED.nist_800_53_refs,
                        evidence_ids     = EXCLUDED.evidence_ids,
                        rationale        = EXCLUDED.rationale,
                        computed_at      = now()
                    """,
                    rows,
                )
        span.set_attribute("cache.row_count", len(rows))


__all__ = [
    "fetch_cached_assessments",
    "persist_evidence",
    "write_cached_assessments",
]
