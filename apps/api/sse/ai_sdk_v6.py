"""
Vercel AI SDK 6 UIMessage stream — typed chunks + LangGraph bridge
==================================================================
Implements the AI SDK 6 data-stream-protocol v1 wire format.

Wire format (verified 2026-05-04 against vercel/ai@6.0.57):
- Response header: ``x-vercel-ai-ui-message-stream: v1``
- Content-Type: ``text/event-stream``
- Body: a sequence of SSE ``data: <json>\\n\\n`` frames terminated by
  ``data: [DONE]\\n\\n``

Chunk types consumed by the frontend's ``useChat`` hook (from the union in
``packages/ai/src/ui-message-stream/ui-message-chunks.ts``):

  start                  — wraps the whole message, may carry messageId
  start-step / finish-step — wraps one LLM/tool step
  text-start, text-delta, text-end — text block with a stable id
  reasoning-start/-delta/-end       — CoT reasoning block (unused Sprint 2)
  tool-input-start, tool-input-delta, tool-input-available, tool-input-error
  tool-output-available, tool-output-error, tool-output-denied
  tool-approval-request — HITL approval (Sprint 6 will wire this)
  source-url, source-document, file — citation surface (Sprint 5/6)
  data-<custom> — typed custom data
  error, abort, message-metadata, finish

Sprint 2 scope: we emit
  start → (per-node: tool-input-available → tool-output-available → text-*) → finish → [DONE]
At Sprint 4 we'll layer true token-level streaming on top via
``graph.astream_events()`` without changing the wire format.

ADR-0003 mapping table was authored against the pre-6.0 spec; this module
supersedes it for the 6.x wire format. ADR-0003 Decision has been amended
to reference this file as the source of truth.

Refs: PLAN.md chunk 2.7; ADR-0003.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

AI_SDK_V6_HEADER = "x-vercel-ai-ui-message-stream"
AI_SDK_V6_VERSION = "v1"
SSE_DONE = "data: [DONE]\n\n"


# ────────────────────────────────────────────────────────────────────────────
# Typed chunk models (subset emitted by Sprint 2 — extensible)
# ────────────────────────────────────────────────────────────────────────────


class _BaseChunk(BaseModel):
    """Base for every AI SDK 6 UIMessage chunk we emit."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StartChunk(_BaseChunk):
    type: Literal["start"] = "start"
    messageId: str
    messageMetadata: dict[str, Any] | None = None


class StartStepChunk(_BaseChunk):
    type: Literal["start-step"] = "start-step"


class FinishStepChunk(_BaseChunk):
    type: Literal["finish-step"] = "finish-step"


class TextStartChunk(_BaseChunk):
    type: Literal["text-start"] = "text-start"
    id: str


class TextDeltaChunk(_BaseChunk):
    type: Literal["text-delta"] = "text-delta"
    id: str
    delta: str


class TextEndChunk(_BaseChunk):
    type: Literal["text-end"] = "text-end"
    id: str


class ToolInputAvailableChunk(_BaseChunk):
    type: Literal["tool-input-available"] = "tool-input-available"
    toolCallId: str
    toolName: str
    input: Any


class ToolOutputAvailableChunk(_BaseChunk):
    type: Literal["tool-output-available"] = "tool-output-available"
    toolCallId: str
    output: Any


class ErrorChunk(_BaseChunk):
    type: Literal["error"] = "error"
    errorText: str


class AbortChunk(_BaseChunk):
    type: Literal["abort"] = "abort"
    reason: str | None = None


class FinishChunk(_BaseChunk):
    type: Literal["finish"] = "finish"
    finishReason: (
        Literal["stop", "length", "content-filter", "tool-calls", "error", "other"]
        | None
    ) = "stop"
    messageMetadata: dict[str, Any] | None = None


def sse_encode(chunk: _BaseChunk) -> str:
    """Serialise a chunk to its SSE wire-format line.

    JSON keys stay camelCase (``messageId``, ``toolCallId``) because that is
    what the AI SDK 6 client validates against. Pydantic's ``model_dump()``
    preserves our field names verbatim; ``exclude_none=True`` trims optional
    fields that were not supplied.
    """

    payload = chunk.model_dump(exclude_none=True, by_alias=True)
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


# ────────────────────────────────────────────────────────────────────────────
# LangGraph-update → UIMessage stream bridge
# ────────────────────────────────────────────────────────────────────────────


async def ui_message_stream_from_graph_updates(
    graph,
    *,
    input: dict[str, Any],
    config: dict[str, Any] | None = None,
    message_id: str | None = None,
    message_metadata: dict[str, Any] | None = None,
    finish_metadata_cb: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
) -> AsyncIterator[str]:
    """Translate ``graph.astream(stream_mode="updates")`` into AI SDK 6 SSE lines.

    Emits, in order:
      1. ``start`` (with messageId + optional messageMetadata)
      2. ``start-step``
      3. For every new tool call on an AIMessage → ``tool-input-available``,
         followed (on the matching ToolMessage) by ``tool-output-available``
      4. For every new human-visible assistant text → ``text-start`` →
         ``text-delta`` → ``text-end``
      5. ``finish-step`` → ``finish`` (with metadata from ``finish_metadata_cb``)
      6. ``[DONE]``

    The caller may plug ``finish_metadata_cb`` to attach late-bound metadata
    (chunk 2.8 uses this to surface the Langfuse trace id once the trace has
    been flushed).
    """

    mid = message_id or f"msg_{uuid.uuid4().hex}"
    yield sse_encode(
        StartChunk(messageId=mid, messageMetadata=message_metadata)
    )
    yield sse_encode(StartStepChunk())

    # Track which messages we've already emitted SSE chunks for — astream(updates)
    # returns per-node deltas, but a node may write multiple messages in one
    # update. A message id appears once (first time), gets emitted, subsequent
    # sightings are skipped.
    emitted_message_ids: set[str] = set()
    # Map tool_call_id -> toolName so we can pair the delayed ToolMessage.
    pending_tool_calls: dict[str, str] = {}

    try:
        async for node_to_update in graph.astream(
            input, config=config, stream_mode="updates"
        ):
            for _node_name, update in node_to_update.items():
                messages = _extract_messages(update)
                for msg in messages:
                    msg_id = _stable_id(msg)
                    if msg_id in emitted_message_ids:
                        continue
                    emitted_message_ids.add(msg_id)

                    # Tool calls embedded in an AIMessage
                    if isinstance(msg, AIMessage):
                        for tc in msg.tool_calls or []:
                            tc_id = tc.get("id") or f"call_{uuid.uuid4().hex}"
                            tc_name = tc.get("name") or "unknown"
                            pending_tool_calls[tc_id] = tc_name
                            yield sse_encode(
                                ToolInputAvailableChunk(
                                    toolCallId=tc_id,
                                    toolName=tc_name,
                                    input=tc.get("args"),
                                )
                            )
                        if msg.content:
                            text_id = f"txt_{uuid.uuid4().hex}"
                            yield sse_encode(TextStartChunk(id=text_id))
                            yield sse_encode(
                                TextDeltaChunk(
                                    id=text_id, delta=str(msg.content)
                                )
                            )
                            yield sse_encode(TextEndChunk(id=text_id))

                    elif isinstance(msg, ToolMessage):
                        tc_id = msg.tool_call_id
                        yield sse_encode(
                            ToolOutputAvailableChunk(
                                toolCallId=tc_id,
                                output=msg.content,
                            )
                        )
                        pending_tool_calls.pop(tc_id, None)
    except Exception:  # noqa: BLE001 — surface any upstream fault
        # IMPORTANT: never put `repr(exc)` on the wire. ``repr()`` of a
        # psycopg / asyncpg / redis-py exception embeds the connection
        # string (with password) into the message — that would leak
        # credentials onto the public SSE channel. Log server-side with
        # full context; surface a static message to the client.
        logger.exception("sse.graph_exception")
        yield sse_encode(
            ErrorChunk(errorText="An internal error occurred while streaming.")
        )
        yield sse_encode(AbortChunk(reason="graph-exception"))
        yield SSE_DONE
        return

    yield sse_encode(FinishStepChunk())

    finish_metadata: dict[str, Any] | None = None
    if finish_metadata_cb is not None:
        finish_metadata = await finish_metadata_cb()

    yield sse_encode(
        FinishChunk(finishReason="stop", messageMetadata=finish_metadata)
    )
    yield SSE_DONE


def _extract_messages(update: Any) -> list[BaseMessage]:
    """Normalise a LangGraph node update into a list of `BaseMessage`.

    Graph node return values can be a plain dict (our orchestrator node), a
    `state.messages` list, or None. Only the messages key is interesting
    here; other state fields flow through the checkpointer.
    """

    if not update:
        return []
    if isinstance(update, dict):
        raw = update.get("messages", [])
    else:
        raw = getattr(update, "messages", [])
    return [m for m in raw if isinstance(m, BaseMessage)]


def _stable_id(msg: BaseMessage) -> str:
    """Return a stable identifier for a message even if `msg.id` is None."""

    if msg.id:
        return msg.id
    return f"{type(msg).__name__}:{hash((msg.content, getattr(msg, 'tool_call_id', None)))}"


__all__ = [
    "AI_SDK_V6_HEADER",
    "AI_SDK_V6_VERSION",
    "AbortChunk",
    "ErrorChunk",
    "FinishChunk",
    "FinishStepChunk",
    "SSE_DONE",
    "StartChunk",
    "StartStepChunk",
    "TextDeltaChunk",
    "TextEndChunk",
    "TextStartChunk",
    "ToolInputAvailableChunk",
    "ToolOutputAvailableChunk",
    "sse_encode",
    "ui_message_stream_from_graph_updates",
]
