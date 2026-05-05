"""
LangGraph graph assembly
========================
Sprint 2 skeleton: a single-node graph that wraps the orchestrator stub.
Sprint 4 will split this into evidence-collection, control-mapping, and
HITL-gate nodes. The seam lives in `build_graph()`; callers never reach into
LangGraph directly so the Sprint 4 refactor is an internal change.

Refs: PLAN.md chunk 2.6; ADR-0001; ADR-0007; system-design 3.2.
"""

from __future__ import annotations

import json
from typing import Any, cast

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model

from apps.api.agents.orchestrator import (
    OrchestratorDeps,
    build_orchestrator_agent,
)
from apps.api.state import (
    SCOPE_REQUIRED_INTENTS,
    AuditPilotState,
    ControlAssessment,
)


_EMPTY_SCOPE_REFUSAL_TEXT = (
    "Pick at least one repo to scan. Open the connector card on your "
    "dashboard and click \"Configure scope\" to choose the repos you want "
    "AuditPilot to read."
)

tracer = trace.get_tracer(__name__)


def build_graph(
    checkpointer: BaseCheckpointSaver,
    *,
    model: Model | str = "test",
):
    """Compile the orchestrator graph against the given checkpointer.

    The returned `CompiledGraph` exposes async APIs (`ainvoke`, `astream`,
    `astream_events`) that chunk 2.7's SSE endpoint consumes. Checkpointing
    is automatic when the caller supplies a ``thread_id`` in the config.
    """

    graph: StateGraph[AuditPilotState] = StateGraph(AuditPilotState)

    async def orchestrator_node(state: AuditPilotState) -> dict[str, Any]:
        """LangGraph node wrapping the Sprint 2 orchestrator stub.

        The caller (the FastAPI `/chat` endpoint) appends the user's
        HumanMessage to state before invoking the graph. This node reads the
        latest human turn, invokes the Pydantic AI agent, and returns only
        the delta (the AI response + any control-map updates). LangGraph's
        default last-writer-wins reducer merges the non-`messages` fields;
        the `add_messages` reducer on `messages` appends the returned list.

        Sprint 3.5 chunk 3.5.5 — empty-scope refusal. When the user-
        supplied intent is in ``SCOPE_REQUIRED_INTENTS`` (currently
        ``run_readiness_scan``) AND ``repo_include_list`` is empty, the
        node short-circuits with a friendly refusal message before any
        LLM call is made. Implements ADR-0015's default-deny on the read
        surface: no scope, no scan.
        """

        if not state.messages:
            return {}

        # Empty-scope guard runs FIRST — before any LLM call — so a
        # mis-configured scan never burns tokens. The return is INSIDE
        # the span context (python-reviewer F3) so the trace records the
        # full path including dict construction.
        if (
            state.intent in SCOPE_REQUIRED_INTENTS
            and not state.repo_include_list
        ):
            with tracer.start_as_current_span("graph.empty_scope_refusal") as span:
                span.set_attribute("scope.required", True)
                span.set_attribute("scope.repo_include_count", 0)
                span.set_attribute("scope.intent", state.intent or "")
                return {
                    "messages": [AIMessage(content=_EMPTY_SCOPE_REFUSAL_TEXT)],
                    "current_step": "empty_scope_refusal",
                    "rejection_reasons": [
                        *state.rejection_reasons,
                        "empty_repo_scope",
                    ],
                }

        with tracer.start_as_current_span("graph.orchestrator_node") as span:
            user_input = cast(str, state.messages[-1].content)
            deps = OrchestratorDeps(
                user_id=state.user_id,
                scan_run_id=state.scan_run_id,
            )
            agent = build_orchestrator_agent(model)
            result = await agent.run(user_input, deps=deps)

            span.set_attribute("orchestrator.output_preview", result.output[:120])
            span.set_attribute(
                "orchestrator.tools_used",
                len(deps.looked_up_controls),
            )

        # Surface Pydantic AI's internal tool call trail as LangChain messages
        # so the SSE bridge (chunk 2.7) can emit `tool-input-available` /
        # `tool-output-available` chunks. Pydantic AI absorbs tool calls
        # internally; without this translation the UI would only see the
        # final assistant text, losing the Tool-card surface (PRD 6.1 FR-059).
        new_lc_messages = _pydantic_ai_to_langchain_messages(result.new_messages())

        control_map_delta: dict[str, ControlAssessment] = {}
        for control in deps.looked_up_controls:
            for tsc_id in control.soc2_tsc_mappings:
                existing = state.control_map.get(tsc_id) or control_map_delta.get(
                    tsc_id
                )
                nist_refs = list(existing.nist_800_53_refs) if existing else []
                if control.id not in nist_refs:
                    nist_refs.append(control.id)
                control_map_delta[tsc_id] = ControlAssessment(
                    tsc_id=tsc_id,
                    status=existing.status if existing else "unknown",
                    confidence=existing.confidence if existing else 0.0,
                    nist_800_53_refs=nist_refs,
                    evidence_ids=list(existing.evidence_ids) if existing else [],
                    rationale=existing.rationale if existing else None,
                )

        return {
            "messages": new_lc_messages,
            "control_map": control_map_delta or state.control_map,
            "current_step": "orchestrator_stub_complete",
        }

    graph.add_node("orchestrator", orchestrator_node)
    graph.add_edge(START, "orchestrator")
    graph.add_edge("orchestrator", END)

    return graph.compile(checkpointer=checkpointer)


def _pydantic_ai_to_langchain_messages(pai_msgs: list) -> list[BaseMessage]:
    """Translate `result.new_messages()` into LangChain primitives.

    Pydantic AI message model:
      ModelRequest(UserPromptPart)  -> skip (user already in graph state)
      ModelResponse(ToolCallPart)   -> AIMessage(content="", tool_calls=[...])
      ModelRequest(ToolReturnPart)  -> ToolMessage(content=..., tool_call_id=...)
      ModelResponse(TextPart)       -> AIMessage(content=...)

    Multiple parts on one message get merged — a single AIMessage carries the
    full tool_calls array so LangChain's `add_messages` reducer preserves the
    tool-call/text ordering the UI expects.
    """

    out: list[BaseMessage] = []
    for m in pai_msgs:
        if isinstance(m, ModelResponse):
            tool_calls: list[dict[str, Any]] = []
            text_chunks: list[str] = []
            for p in m.parts:
                if isinstance(p, ToolCallPart):
                    tool_calls.append(
                        {
                            "id": p.tool_call_id,
                            "name": p.tool_name,
                            "args": p.args if isinstance(p.args, dict) else _safe_json(p.args),
                        }
                    )
                elif isinstance(p, TextPart):
                    text_chunks.append(p.content)
            if tool_calls or text_chunks:
                out.append(
                    AIMessage(
                        content="".join(text_chunks),
                        tool_calls=tool_calls,
                    )
                )
        elif isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, ToolReturnPart):
                    out.append(
                        ToolMessage(
                            content=_coerce_tool_return_content(p.content),
                            tool_call_id=p.tool_call_id,
                            name=p.tool_name,
                        )
                    )
                # UserPromptPart is already in state; don't duplicate it.
    return out


def _safe_json(value: Any) -> dict[str, Any]:
    """Coerce a tool-call args payload into a dict for the LangChain tool_calls schema."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    return {"raw": value}


def _coerce_tool_return_content(value: Any) -> str:
    """LangChain's ToolMessage content is a string; serialise JSON-ish returns."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)


__all__ = ["build_graph"]
