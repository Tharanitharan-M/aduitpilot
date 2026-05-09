"""A2A v1.0 JSON-RPC task wire shapes.

The auditor exposes ``POST /a2a`` as a JSON-RPC 2.0 endpoint with two
methods: ``SendMessage`` (creates or continues a task) and ``GetTask``
(polls a task by id). Per A2A v1.0 §9.4 these are the minimum the
orchestrator needs for the mock-readiness-challenge flow.

Refs: PLAN.md Sprint 8 chunks 8.3 / 8.5; A2A spec §9.4.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str | None = None
    media_type: str = Field(default="text/plain", alias="mediaType")
    data: dict[str, Any] | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    message_id: str = Field(alias="messageId")
    role: Literal["ROLE_USER", "ROLE_AGENT", "ROLE_SYSTEM"] = "ROLE_USER"
    parts: list[Part] = Field(default_factory=list)


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    artifact_id: str = Field(alias="artifactId")
    name: str = ""
    parts: list[Part] = Field(default_factory=list)


class TaskStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    state: Literal[
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_WORKING",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_BUDGET_EXCEEDED",
    ] = "TASK_STATE_SUBMITTED"
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    error: str | None = None


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    context_id: str = Field(default="", alias="contextId")
    status: TaskStatus = Field(default_factory=TaskStatus)
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)


class JsonRpcError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request envelope.

    ``method`` is a free-form string at the schema layer so unknown
    methods can be returned with the proper ``-32601 method-not-found``
    error code (per JSON-RPC 2.0). Validating against a ``Literal``
    here would 422 on the unknown name and surface a less actionable
    ``-32600 invalid-request`` to the client.
    """

    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    id: int | str | None = None


class JsonRpcResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    result: dict[str, Any] | None = None
    error: JsonRpcError | None = None
    id: int | str | None = None


__all__ = [
    "Artifact",
    "JsonRpcError",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "Message",
    "Part",
    "Task",
    "TaskStatus",
]
