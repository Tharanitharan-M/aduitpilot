"""Sprint 8 chunk 8.3 — AgentCard Ed25519 sign + verify round trip."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.auditor.a2a.agent_card import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    canonicalize_card,
    sign_agent_card,
    verify_agent_card,
)


def _fresh_keypair() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    private_hex = private_key.private_bytes_raw().hex()
    public_hex = private_key.public_key().public_bytes_raw().hex()
    return private_hex, public_hex


def _sample_card() -> AgentCard:
    return AgentCard(
        id="urn:test:adversarial",
        name="Test Adversarial",
        version="0.1.0",
        description="Test",
        capabilities=AgentCapabilities(),
        skills=[
            AgentSkill(
                id="readiness-redteam",
                name="Readiness Red-Team",
                description="Test skill",
            )
        ],
        interfaces=[AgentInterface(transport="JSONRPC", url="http://localhost:8001/a2a")],
    )


def test_canonicalize_strips_signature_and_sorts_keys() -> None:
    card = _sample_card()
    body = json.loads(canonicalize_card(card).decode())
    assert "signature" not in body
    keys = list(body.keys())
    assert keys == sorted(keys)


def test_sign_and_verify_round_trip() -> None:
    private_hex, public_hex = _fresh_keypair()
    card = _sample_card()
    signed = sign_agent_card(card, private_hex)
    assert signed.signature is not None
    assert signed.signature.public_key == public_hex
    assert verify_agent_card(signed, expected_public_key_hex=public_hex)


def test_verify_rejects_pubkey_mismatch() -> None:
    private_hex, _ = _fresh_keypair()
    _, other_public = _fresh_keypair()
    signed = sign_agent_card(_sample_card(), private_hex)
    assert not verify_agent_card(signed, expected_public_key_hex=other_public)


def test_verify_rejects_tampered_card() -> None:
    private_hex, public_hex = _fresh_keypair()
    signed = sign_agent_card(_sample_card(), private_hex)
    tampered = signed.model_copy(update={"name": "Tampered"})
    assert not verify_agent_card(tampered, expected_public_key_hex=public_hex)


def test_verify_returns_false_for_unsigned_card() -> None:
    assert not verify_agent_card(_sample_card(), expected_public_key_hex="00" * 32)


def test_sign_rejects_short_key() -> None:
    with pytest.raises(ValueError):
        sign_agent_card(_sample_card(), "deadbeef")
