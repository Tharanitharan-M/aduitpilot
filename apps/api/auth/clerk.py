"""
Clerk JWT verification — FastAPI dependency (Sprint 3 chunk 3.5).

Verifies the Clerk session JWT forwarded as ``Authorization: Bearer <token>``
by the Next.js frontend. Uses JWKS from Clerk's well-known endpoint so the
public key rotates automatically.

Security properties:
- ``algorithm="RS256"`` only — no HS256 confusion attacks.
- ``leeway=10`` absorbs reasonable clock skew without opening a wide window.
- Token validated against the configured Clerk issuer; tokens from other
  Clerk apps are rejected.
- ``PyJWKClient`` is cached at the process level (per JWKS URL) so the
  blocking key-set fetch is made at most once per cold start, not per request.
- All blocking JWT / JWKS operations are wrapped in ``asyncio.to_thread``
  so the uvicorn event loop is never blocked.

Refs: PLAN.md chunk 3.5, ADR-0008, system-design §6.1.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from opentelemetry import trace
from pydantic import BaseModel

from apps.api.config import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

_bearer = HTTPBearer(auto_error=True)


class ClerkUser(BaseModel):
    """Typed representation of the claims we extract from a Clerk JWT."""

    user_id: str
    session_id: str
    org_id: str | None = None
    is_demo: bool = False


@functools.lru_cache(maxsize=8)
def _get_cached_jwks_client(jwks_url: str) -> PyJWKClient:
    """Return a long-lived, process-cached ``PyJWKClient`` for the given URL.

    ``lru_cache`` ensures a single ``PyJWKClient`` instance per unique JWKS
    endpoint, so the in-memory key-set cache inside ``PyJWKClient`` is
    reused across requests rather than thrown away after each auth call.
    """
    return PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)


def _build_jwks_url(settings: Settings) -> str:
    """Return the Clerk JWKS URL from settings.

    Reads ``clerk_jwks_url`` directly if set, otherwise derives it from
    ``clerk_publishable_key`` using the documented Clerk URL format.
    The ``clerk_jwks_url`` setting (CLERK_JWKS_URL env var) is the
    authoritative source; set it explicitly in all deployed environments.
    """
    # Prefer explicit JWKS URL from settings (set via CLERK_JWKS_URL env var)
    if settings.clerk_jwks_url:
        return settings.clerk_jwks_url

    # Fallback: derive from publishable key (works for test/dev keys only)
    pk = settings.clerk_publishable_key
    if pk.startswith("pk_test_"):
        # pk_test_<base64-encoded-frontend-api-host>$
        import base64
        encoded = pk.split("pk_test_")[1].rstrip("$")
        # PyJWT/Clerk convention: the encoded portion is the frontend API host
        try:
            frontend_api = base64.b64decode(encoded + "==").decode().rstrip("$")
            return f"https://{frontend_api}/.well-known/jwks.json"
        except Exception:  # noqa: BLE001
            pass
    # Final fallback — will fail loudly if wrong
    return f"https://clerk.{pk}.com/.well-known/jwks.json"


async def _verify_token_thread(token: str, jwks_url: str, issuer_url: str | None = None) -> dict[str, Any]:
    """Blocking JWT decode offloaded to a thread pool via asyncio.to_thread.

    ``PyJWKClient.get_signing_key_from_jwt`` makes a synchronous HTTP call on
    cache miss. Wrapping the whole verify in a thread ensures the uvicorn
    event loop is never blocked — even on the first request after a cold start.
    """
    def _sync_verify() -> dict[str, Any]:
        client = _get_cached_jwks_client(jwks_url)
        signing_key = client.get_signing_key_from_jwt(token)
        decode_kwargs: dict[str, Any] = {
            "algorithms": ["RS256"],
            "leeway": 10,
            "options": {"require": ["sub", "sid", "exp", "iat"]},
        }
        if issuer_url:
            decode_kwargs["issuer"] = issuer_url
        return jwt.decode(  # type: ignore[return-value]
            token,
            signing_key.key,
            **decode_kwargs,
        )

    return await asyncio.to_thread(_sync_verify)


async def verify_clerk_token(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    settings: Settings = Depends(lambda: Settings()),
) -> ClerkUser:
    """FastAPI dependency: verify Clerk JWT, return typed ClerkUser.

    Raises HTTP 401 on any verification failure. Intentionally vague error
    messages to avoid leaking JWT internals to callers.
    """
    token = credentials.credentials

    with tracer.start_as_current_span("auth.verify_clerk_token") as span:
        try:
            jwks_url = _build_jwks_url(settings)
            issuer_url = settings.clerk_issuer_url or None
            span.set_attribute("auth.jwks_url", jwks_url)
            payload = await _verify_token_thread(token, jwks_url, issuer_url)
        except jwt.ExpiredSignatureError:
            span.set_attribute("auth.error", "token_expired")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
            )
        except (jwt.PyJWTError, httpx.HTTPError, Exception) as exc:  # noqa: BLE001
            logger.debug("clerk_jwt.invalid reason=%s", exc)
            span.set_attribute("auth.error", "invalid_token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
            )

        user = ClerkUser(
            user_id=payload["sub"],
            session_id=payload["sid"],
            org_id=payload.get("org_id"),
            is_demo=payload.get("is_demo", False),
        )
        span.set_attribute("auth.user_id", user.user_id)
        return user
