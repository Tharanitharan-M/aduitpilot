"""Sprint 8 chunk 8.1 / 8.3 / 8.5 — auditor FastAPI surface."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from apps.auditor.a2a.agent_card import (
    AgentCard,
    verify_agent_card,
)
from apps.auditor.agents.adversarial import AdversarialResult
from apps.auditor.config import get_settings


@pytest.fixture
def signing_keys() -> tuple[str, str]:
    pk = Ed25519PrivateKey.generate()
    return pk.private_bytes_raw().hex(), pk.public_key().public_bytes_raw().hex()


@pytest.fixture
def auditor_client(
    monkeypatch: pytest.MonkeyPatch, signing_keys: tuple[str, str]
) -> Iterator[TestClient]:
    private_hex, _ = signing_keys
    monkeypatch.setenv("A2A_PRIVATE_KEY", private_hex)
    # Point the prompt loader at the real apps/api/agents/prompts dir.
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv(
        "AUDITOR_PROMPT_DIR", str(repo_root / "apps" / "api" / "agents" / "prompts")
    )
    get_settings.cache_clear()  # type: ignore[attr-defined]

    # Stub the agent so SendMessage doesn't try to call out to a real LLM.
    from apps.auditor import main as auditor_main

    async def _fake_run_adversarial(*, agent, scan_context, cap_usd):  # type: ignore[no-untyped-def]
        return AdversarialResult(
            summary=f"stub for {len(scan_context)} keys",
            findings=[],
            budget={"spent_usd": 0.001, "cap_usd": cap_usd, "calls": 1},
            status="completed",
        )

    monkeypatch.setattr(auditor_main, "run_adversarial", _fake_run_adversarial)

    with TestClient(auditor_main.app) as client:
        yield client


def test_health(auditor_client: TestClient) -> None:
    response = auditor_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "auditor"


def test_agent_card_signed_and_verifies(
    auditor_client: TestClient, signing_keys: tuple[str, str]
) -> None:
    _, public_hex = signing_keys
    response = auditor_client.get("/.well-known/agent.json")
    assert response.status_code == 200
    card = AgentCard.model_validate(response.json())
    assert card.signature is not None
    assert card.signature.public_key == public_hex
    assert verify_agent_card(card, expected_public_key_hex=public_hex)


def test_agent_card_signature_rejected_under_pubkey_swap(
    auditor_client: TestClient,
) -> None:
    response = auditor_client.get("/.well-known/agent.json")
    card = AgentCard.model_validate(response.json())
    bogus_pubkey = "00" * 32
    assert not verify_agent_card(card, expected_public_key_hex=bogus_pubkey)


def test_jsonrpc_send_message_returns_completed_task(
    auditor_client: TestClient,
) -> None:
    body = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "msg-1",
                "role": "ROLE_USER",
                "parts": [
                    {
                        "mediaType": "application/json",
                        "data": {"control_map": [], "evidence": []},
                    }
                ],
            }
        },
        "id": 1,
    }
    response = auditor_client.post("/a2a", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    task = payload["result"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert len(task["artifacts"]) == 1
    artifact = task["artifacts"][0]
    inner = artifact["parts"][0]["data"]
    assert inner["status"] == "completed"
    assert "stub for 2 keys" in inner["summary"]


def test_jsonrpc_get_task_returns_stored_task(auditor_client: TestClient) -> None:
    send_body = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "msg-2",
                "role": "ROLE_USER",
                "parts": [{"mediaType": "application/json", "data": {"k": 1}}],
            }
        },
        "id": "abc",
    }
    send_response = auditor_client.post("/a2a", json=send_body)
    task_id = send_response.json()["result"]["id"]

    get_body = {
        "jsonrpc": "2.0",
        "method": "GetTask",
        "params": {"id": task_id},
        "id": 99,
    }
    response = auditor_client.post("/a2a", json=get_body)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 99
    assert body["result"]["id"] == task_id


def test_jsonrpc_unknown_method_returns_method_not_found(
    auditor_client: TestClient,
) -> None:
    body = {"jsonrpc": "2.0", "method": "DoTheThing", "params": {}, "id": 5}
    response = auditor_client.post("/a2a", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32601


def test_jsonrpc_send_message_rejects_missing_data_part(
    auditor_client: TestClient,
) -> None:
    body = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "msg-3",
                "role": "ROLE_USER",
                "parts": [{"text": "no json here", "mediaType": "text/plain"}],
            }
        },
        "id": 7,
    }
    response = auditor_client.post("/a2a", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32602


def test_jsonrpc_text_part_with_json_falls_back(auditor_client: TestClient) -> None:
    body = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "msg-4",
                "role": "ROLE_USER",
                "parts": [
                    {
                        "text": json.dumps({"control_map": []}),
                        "mediaType": "text/plain",
                    }
                ],
            }
        },
        "id": 8,
    }
    response = auditor_client.post("/a2a", json=body)
    assert response.status_code == 200
    assert response.json()["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
