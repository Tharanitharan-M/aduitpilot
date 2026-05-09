"""Sprint 8 chunk 8.4 — RemoteA2aAgent client behaviour."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.api.agents.remote_auditor import (
    AgentCardSignatureError,
    RemoteA2aAgent,
    RemoteAuditorError,
)
from apps.auditor.a2a.agent_card import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    sign_agent_card,
)


def _signed_card(public_url: str, private_hex: str) -> AgentCard:
    card = AgentCard(
        id="urn:test:adversarial",
        name="Test Adversarial",
        version="0.1.0",
        description="Test",
        capabilities=AgentCapabilities(),
        skills=[AgentSkill(id="x", name="x", description="x")],
        interfaces=[AgentInterface(transport="JSONRPC", url=f"{public_url}/a2a")],
    )
    return sign_agent_card(card, private_hex)


def _completed_task_response(task_id: str, summary: str = "ok") -> dict:
    return {
        "id": task_id,
        "contextId": "ctx",
        "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-05-08T00:00:00Z"},
        "artifacts": [
            {
                "artifactId": "art-1",
                "name": "adversarial-result",
                "parts": [
                    {
                        "mediaType": "application/json",
                        "data": {
                            "summary": summary,
                            "findings": [
                                {
                                    "severity": "high",
                                    "tsc_id": "CC6.6",
                                    "objection": "Test objection",
                                    "recommended_next_step": "Do thing",
                                }
                            ],
                            "budget": {"spent_usd": 0.001, "cap_usd": 0.5, "calls": 1},
                            "status": "completed",
                        },
                    }
                ],
            }
        ],
        "history": [],
    }


@pytest.fixture
def keypair() -> tuple[str, str]:
    pk = Ed25519PrivateKey.generate()
    return pk.private_bytes_raw().hex(), pk.public_key().public_bytes_raw().hex()


@pytest.mark.asyncio
async def test_send_message_round_trip(keypair: tuple[str, str]) -> None:
    private_hex, public_hex = keypair
    base_url = "http://auditor.test"
    signed_card = _signed_card(base_url, private_hex)

    captured_requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(
                200, json=signed_card.model_dump(by_alias=True, exclude_none=True)
            )
        if request.url.path == "/a2a":
            body = json.loads(request.content)
            captured_requests.append((body["method"], body["params"]))
            assert body["method"] == "SendMessage"
            task_id = uuid.uuid4().hex
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "result": _completed_task_response(task_id),
                    "id": body["id"],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        agent = RemoteA2aAgent(
            base_url=base_url,
            expected_public_key_hex=public_hex,
            http_client=client,
        )
        result = await agent.send_message({"control_map": [], "evidence": []})

    assert result.state == "TASK_STATE_COMPLETED"
    assert result.summary == "ok"
    assert len(result.findings) == 1
    assert result.findings[0]["severity"] == "high"
    assert captured_requests[0][0] == "SendMessage"


@pytest.mark.asyncio
async def test_send_message_rejects_pubkey_mismatch(keypair: tuple[str, str]) -> None:
    private_hex, _ = keypair
    other_pk = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    base_url = "http://auditor.test"
    signed_card = _signed_card(base_url, private_hex)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(
                200, json=signed_card.model_dump(by_alias=True, exclude_none=True)
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        agent = RemoteA2aAgent(
            base_url=base_url,
            expected_public_key_hex=other_pk,
            http_client=client,
        )
        with pytest.raises(AgentCardSignatureError):
            await agent.send_message({})


@pytest.mark.asyncio
async def test_send_message_caches_card(keypair: tuple[str, str]) -> None:
    private_hex, public_hex = keypair
    base_url = "http://auditor.test"
    signed_card = _signed_card(base_url, private_hex)
    card_fetches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal card_fetches
        if request.url.path == "/.well-known/agent.json":
            card_fetches += 1
            return httpx.Response(
                200, json=signed_card.model_dump(by_alias=True, exclude_none=True)
            )
        if request.url.path == "/a2a":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "result": _completed_task_response(uuid.uuid4().hex),
                    "id": body["id"],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        agent = RemoteA2aAgent(
            base_url=base_url,
            expected_public_key_hex=public_hex,
            http_client=client,
        )
        await agent.send_message({})
        await agent.send_message({})
    assert card_fetches == 1


@pytest.mark.asyncio
async def test_send_message_propagates_jsonrpc_error(keypair: tuple[str, str]) -> None:
    private_hex, public_hex = keypair
    base_url = "http://auditor.test"
    signed_card = _signed_card(base_url, private_hex)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(
                200, json=signed_card.model_dump(by_alias=True, exclude_none=True)
            )
        if request.url.path == "/a2a":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": "boom"},
                    "id": body["id"],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        agent = RemoteA2aAgent(
            base_url=base_url,
            expected_public_key_hex=public_hex,
            http_client=client,
        )
        with pytest.raises(RemoteAuditorError) as info:
            await agent.send_message({})
        assert "boom" in str(info.value)
