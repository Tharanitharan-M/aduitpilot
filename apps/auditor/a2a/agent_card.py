"""A2A v1.0 AgentCard with Ed25519 signature support.

The AgentCard is the discovery document at
``GET /.well-known/agent.json`` (per A2A v1.0 §2.2 / §4.4.1). Clients use
it to learn the agent's identity, supported capabilities, JSON-RPC
endpoint, and the public key with which to verify the card itself.

We canonicalize the card to JSON Canonicalization Scheme (JCS-style:
sorted keys, no insignificant whitespace) before signing per A2A §8.4.1.
That gives us deterministic byte-for-byte verification across
serialisers.

Refs: PLAN.md Sprint 8 chunk 8.3; ADR-0002; A2A spec §4.4 / §8.4.
"""

from __future__ import annotations

import base64
import json
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field

SIGNATURE_FIELD = "signature"


class AgentCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    streaming: bool = False
    push_notifications: bool = Field(default=False, alias="pushNotifications")
    extended_agent_card: bool = Field(default=False, alias="extendedAgentCard")


class AgentSkill(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AgentInterface(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    transport: Literal["JSONRPC", "HTTP+JSON"] = "JSONRPC"
    url: str


class AgentCardSignature(BaseModel):
    """Detached Ed25519 signature over the canonical JSON of the card."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    algorithm: Literal["Ed25519"] = "Ed25519"
    public_key: str = Field(
        alias="publicKey",
        description="Ed25519 public key as 64-character lowercase hex.",
    )
    signature: str = Field(
        description="Base64-encoded raw 64-byte Ed25519 signature.",
    )


class AgentCard(BaseModel):
    """A2A v1.0 AgentCard discovery document.

    ``issued_at`` is included in the canonicalized payload that gets
    signed so a captured card cannot be replayed indefinitely. The
    orchestrator's verifier can reject cards older than a configurable
    TTL (default 7 days; fast enough rotation for service-to-service
    use, long enough to avoid clock-skew false negatives).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    name: str
    version: str
    description: str
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    skills: list[AgentSkill] = Field(default_factory=list)
    interfaces: list[AgentInterface] = Field(default_factory=list)
    issued_at: str | None = Field(
        default=None,
        alias="issuedAt",
        description="RFC 3339 issuance timestamp; required for production use.",
    )
    signature: AgentCardSignature | None = None


def canonicalize_card(card: AgentCard | dict) -> bytes:
    """Return the canonical UTF-8 JSON bytes of ``card`` minus its signature.

    Sorted keys + no whitespace gives the same bytes regardless of which
    side serialised the document — the sign-and-verify round-trip would
    fail without it because Pydantic and ``json.dumps`` order keys
    differently.
    """

    if isinstance(card, AgentCard):
        body = card.model_dump(by_alias=True, exclude_none=True)
    else:
        body = dict(card)
    body.pop(SIGNATURE_FIELD, None)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_agent_card(card: AgentCard, private_key_hex: str) -> AgentCard:
    """Return a copy of ``card`` with an Ed25519 signature attached.

    ``private_key_hex`` is the 32-byte raw Ed25519 seed as 64 hex chars
    (the format ``cryptography`` accepts via ``from_private_bytes``).
    Stamps a fresh ``issued_at`` timestamp before signing so a captured
    card cannot be replayed past its TTL.
    """

    from datetime import UTC, datetime

    raw = bytes.fromhex(private_key_hex.strip())
    if len(raw) != 32:
        raise ValueError("Ed25519 private key must be exactly 32 bytes (64 hex chars)")
    private_key = Ed25519PrivateKey.from_private_bytes(raw)
    public_key_hex = private_key.public_key().public_bytes_raw().hex()

    stamped = card.model_copy(
        update={"issued_at": datetime.now(UTC).isoformat(timespec="seconds")}
    )
    canonical = canonicalize_card(stamped)
    sig = private_key.sign(canonical)
    return stamped.model_copy(
        update={
            "signature": AgentCardSignature(
                algorithm="Ed25519",
                public_key=public_key_hex,
                signature=base64.b64encode(sig).decode("ascii"),
            )
        }
    )


DEFAULT_CARD_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


def verify_agent_card(
    card: AgentCard,
    *,
    expected_public_key_hex: str | None = None,
    max_age_seconds: int | None = DEFAULT_CARD_TTL_SECONDS,
) -> bool:
    """Verify the Ed25519 signature on ``card``.

    When ``expected_public_key_hex`` is provided, the card's embedded
    public key must equal it — this is the "pinned public key" check
    the orchestrator uses so a tampered AgentCard cannot swap in its
    own pubkey along with a matching signature.

    When ``max_age_seconds`` is provided (default 7 days), reject cards
    whose ``issued_at`` is older than the limit. This bounds the replay
    window. Pass ``None`` to skip the freshness check (useful in tests).
    """

    from datetime import UTC, datetime

    if card.signature is None:
        return False
    sig_block = card.signature
    if (
        expected_public_key_hex is not None
        and sig_block.public_key.lower().strip()
        != expected_public_key_hex.lower().strip()
    ):
        return False
    if max_age_seconds is not None and card.issued_at:
        try:
            issued = datetime.fromisoformat(card.issued_at)
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - issued).total_seconds()
            if age > max_age_seconds:
                return False
        except ValueError:
            return False
    try:
        public_key_bytes = bytes.fromhex(sig_block.public_key)
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        signature_bytes = base64.b64decode(sig_block.signature)
    except (ValueError, TypeError):
        return False
    canonical = canonicalize_card(card)
    try:
        public_key.verify(signature_bytes, canonical)
    except InvalidSignature:
        return False
    return True


__all__ = [
    "AgentCapabilities",
    "AgentCard",
    "AgentCardSignature",
    "AgentInterface",
    "AgentSkill",
    "canonicalize_card",
    "sign_agent_card",
    "verify_agent_card",
]
