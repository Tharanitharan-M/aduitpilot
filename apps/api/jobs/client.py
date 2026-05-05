"""Redis client factory and Upstash REST shim.

The queue is written against a narrow :class:`RedisLike` protocol that
``redis.asyncio.Redis`` (and therefore ``fakeredis.FakeAsyncRedis``)
already satisfies. For production with an Upstash REST URL we provide
:class:`UpstashRestRedis`, which implements the same method surface by
POSTing command arrays to the REST endpoint.

Both clients normalise responses to **the redis-py shape** so the queue
code does not branch on transport.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import httpx

from apps.api.config import Settings


class RedisLike(Protocol):
    """Narrow async Redis surface the queue depends on."""

    async def xadd(self, stream: str, fields: Mapping[str, Any]) -> str: ...
    async def xgroup_create(
        self, stream: str, group: str, id: str = "0", mkstream: bool = True
    ) -> Any: ...
    async def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: Mapping[str, str],
        count: int = 1,
        block: int | None = None,
    ) -> Any: ...
    async def xpending_range(
        self,
        stream: str,
        group: str,
        min: str,
        max: str,
        count: int,
        consumername: str | None = None,
        idle: int | None = None,
    ) -> Any: ...
    async def xclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        min_idle_time: int,
        message_ids: Sequence[str],
    ) -> Any: ...
    async def xack(self, stream: str, group: str, *ids: str) -> int: ...
    async def exists(self, key: str) -> int: ...
    async def setex(self, key: str, ttl: int, value: str) -> Any: ...
    async def delete(self, *keys: str) -> int: ...
    async def aclose(self) -> None: ...


# ─── Upstash REST adapter ────────────────────────────────────────────────────
#
# Upstash exposes every Redis command at ``POST <base>/`` with a JSON body of
# ``["COMMAND", "arg", ...]``. Responses are ``{"result": <value>}``. We
# reshape those responses to match redis-py so ``JobQueue`` code is transport-
# agnostic.


class UpstashRestRedis:
    """redis-py-compatible async client backed by the Upstash REST API."""

    def __init__(self, url: str, token: str, *, timeout: float = 10.0) -> None:
        self._url = url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _exec(self, *command: Any) -> Any:
        response = await self._http.post(self._url, json=[str(x) for x in command])
        # Sprint 3 day-1 chunk 3.0f — inspect the JSON body BEFORE
        # ``raise_for_status``. Upstash REST returns HTTP 400 with body
        # ``{"error": "BUSYGROUP Consumer Group name already exists"}`` for
        # benign "already exists" cases (e.g. XGROUP CREATE on an existing
        # group, which fires on every uvicorn restart after the first). If
        # we let ``raise_for_status`` fire first those become
        # ``HTTPStatusError`` and the BUSYGROUP swallow in ``xgroup_create``
        # never executes — the worker silently fails to start. Reading the
        # body first preserves the redis-py-shaped ``RuntimeError("Upstash
        # error: ...")`` contract that callers depend on.
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and "error" in payload:
            raise RuntimeError(f"Upstash error: {payload['error']}")
        response.raise_for_status()
        return payload.get("result") if isinstance(payload, dict) else None

    async def aclose(self) -> None:
        await self._http.aclose()

    # ─── Streams ──────────────────────────────────────────────────────────────
    async def xadd(self, stream: str, fields: Mapping[str, Any]) -> str:
        flattened: list[Any] = []
        for k, v in fields.items():
            flattened.extend([k, v])
        return await self._exec("XADD", stream, "*", *flattened)

    async def xgroup_create(
        self, stream: str, group: str, id: str = "0", mkstream: bool = True
    ) -> Any:
        args = ["XGROUP", "CREATE", stream, group, id]
        if mkstream:
            args.append("MKSTREAM")
        try:
            return await self._exec(*args)
        except RuntimeError as exc:
            if "BUSYGROUP" in str(exc):
                return "OK"
            raise

    async def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: Mapping[str, str],
        count: int = 1,
        block: int | None = None,
    ) -> Any:
        args: list[Any] = ["XREADGROUP", "GROUP", group, consumer]
        if count:
            args.extend(["COUNT", str(count)])
        if block is not None:
            args.extend(["BLOCK", str(block)])
        args.append("STREAMS")
        args.extend(streams.keys())
        args.extend(streams.values())
        raw = await self._exec(*args)
        if raw is None:
            return []
        return [
            (
                stream_name,
                [(mid, _pairs_to_dict(pairs)) for mid, pairs in entries],
            )
            for stream_name, entries in raw
        ]

    async def xpending_range(
        self,
        stream: str,
        group: str,
        min: str,
        max: str,
        count: int,
        consumername: str | None = None,
        idle: int | None = None,
    ) -> Any:
        args: list[Any] = ["XPENDING", stream, group]
        if idle is not None:
            args.extend(["IDLE", str(idle)])
        args.extend([min, max, str(count)])
        if consumername:
            args.append(consumername)
        raw = await self._exec(*args) or []
        return [
            {
                "message_id": mid,
                "consumer": consumer,
                "time_since_delivered": idle_ms,
                "times_delivered": deliveries,
            }
            for mid, consumer, idle_ms, deliveries in raw
        ]

    async def xclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        min_idle_time: int,
        message_ids: Sequence[str],
    ) -> Any:
        args = ["XCLAIM", stream, group, consumer, str(min_idle_time), *message_ids]
        raw = await self._exec(*args) or []
        return [(mid, _pairs_to_dict(pairs)) for mid, pairs in raw]

    async def xack(self, stream: str, group: str, *ids: str) -> int:
        return int(await self._exec("XACK", stream, group, *ids))

    # ─── Strings / keys ───────────────────────────────────────────────────────
    async def exists(self, key: str) -> int:
        return int(await self._exec("EXISTS", key))

    async def setex(self, key: str, ttl: int, value: str) -> Any:
        return await self._exec("SETEX", key, str(ttl), value)

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await self._exec("DEL", *keys))


def _pairs_to_dict(pairs: Sequence[Any] | Mapping[str, Any] | None) -> dict[str, Any]:
    """Collapse a flat ``[k, v, k, v, ...]`` list into a dict.

    redis-py returns fields as a dict; Upstash REST returns them as a flat
    list. Tolerate either shape.
    """

    if pairs is None:
        return {}
    if isinstance(pairs, Mapping):
        return dict(pairs)
    return {pairs[i]: pairs[i + 1] for i in range(0, len(pairs) - 1, 2)}


def make_redis_client(settings: Settings) -> RedisLike:
    """Return a redis-py-compatible async client for the configured URL.

    * ``redis://`` / ``rediss://`` / ``unix://`` → ``redis.asyncio.Redis``
    * ``http://`` / ``https://`` → :class:`UpstashRestRedis`
    """

    # `redis_url` is a SecretStr (Sprint 3 day-0 chunk 3.0c). Unwrap once
    # at the very last moment — never log or stringify the raw URL.
    url = settings.redis_url.get_secret_value()
    if url.startswith(("redis://", "rediss://", "unix://")):
        import redis.asyncio as redis_asyncio

        return redis_asyncio.from_url(url, decode_responses=True)
    if url.startswith(("http://", "https://")):
        secret = settings.upstash_redis_rest_token
        if not secret:
            raise ValueError(
                "UPSTASH_REDIS_REST_TOKEN is required when REDIS_URL is an HTTP(S) Upstash endpoint"
            )
        token = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
        return UpstashRestRedis(url, token)
    # IMPORTANT: do not include `url` in the error message — it embeds the
    # password. Mention the scheme only.
    scheme = url.split("://", 1)[0] if "://" in url else "<no-scheme>"
    raise ValueError(f"Unsupported REDIS_URL scheme: {scheme!r}")
