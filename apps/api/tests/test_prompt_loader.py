"""PromptLoader contract (chunk 2.12, ADR-0011).

The loader must:

* Prefer Langfuse when reachable.
* Fall back to local YAML on timeout or any other SDK error.
* Raise :class:`LoaderError` if neither path produces a prompt.
* Honour the in-memory TTL cache so repeated calls don't thrash Langfuse.
* Emit an observability event every time we fall back, so ops can alert.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.api.agents.prompts import (
    CompiledPrompt,
    LoaderError,
    PromptDefinition,
    PromptLoader,
    PromptSource,
)

pytestmark = pytest.mark.asyncio


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def prompt_dir(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    (d / "orchestrator").mkdir(parents=True)
    (d / "orchestrator" / "production.yaml").write_text(
        """\
name: orchestrator
version: 7
model: gemini-2.5-flash-lite
temperature: 0.0
max_tokens: 2048
system: |
  LOCAL system prompt for tests.
guardrails:
  max_turns: 10
  cost_cap_usd: 0.10
  delimiter_evidence: true
metadata:
  reason_for_change: "test fixture"
"""
    )
    return d


class _FakeLangfuse:
    """Records calls and returns either a fake TextPromptClient or raises."""

    def __init__(self, *, payload=None, exc: Exception | None = None, delay: float = 0.0) -> None:
        self.payload = payload
        self.exc = exc
        self.delay = delay
        self.calls: list[tuple[str, str]] = []

    def get_prompt(self, name: str, *, label: str):
        self.calls.append((name, label))
        if self.delay:
            import time

            time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.payload


def _langfuse_prompt(prompt_text: str, *, version: int, name: str = "orchestrator"):
    return SimpleNamespace(
        prompt=prompt_text,
        version=version,
        config={
            "model": "gemini-2.5-flash-lite",
            "temperature": 0.0,
            "max_tokens": 4096,
            "name": name,
            "metadata": {"source": "langfuse"},
        },
    )


# ─── Tests ───────────────────────────────────────────────────────────────────


async def test_load_returns_langfuse_prompt_when_reachable(prompt_dir: Path) -> None:
    fake = _FakeLangfuse(
        payload=_langfuse_prompt("LANGFUSE system prompt v3", version=3)
    )
    loader = PromptLoader(fake, local_dir=prompt_dir)

    result = await loader.load("orchestrator")

    assert isinstance(result, CompiledPrompt)
    assert result.source == PromptSource.LANGFUSE
    assert result.definition.version == 3
    assert result.system == "LANGFUSE system prompt v3"
    assert fake.calls == [("orchestrator", "production")]


async def test_load_falls_back_to_local_on_langfuse_timeout(prompt_dir: Path) -> None:
    events: list[tuple[str, str, dict]] = []
    fake = _FakeLangfuse(delay=1.0)  # slower than the 0.05s timeout below
    loader = PromptLoader(
        fake,
        local_dir=prompt_dir,
        fetch_timeout_seconds=0.05,
        retries=0,
        observability_hook=lambda e, n, c: events.append((e, n, c)),
    )

    result = await loader.load("orchestrator")

    assert result.source == PromptSource.LOCAL
    assert result.definition.version == 7  # the local fixture's version
    assert any(e[0] == "langfuse_fallback" for e in events), events


async def test_load_falls_back_on_langfuse_exception(prompt_dir: Path) -> None:
    fake = _FakeLangfuse(exc=RuntimeError("Langfuse 500"))
    events: list[tuple[str, str, dict]] = []
    loader = PromptLoader(
        fake,
        local_dir=prompt_dir,
        retries=0,
        observability_hook=lambda e, n, c: events.append((e, n, c)),
    )

    result = await loader.load("orchestrator")

    assert result.source == PromptSource.LOCAL
    assert result.system.startswith("LOCAL system prompt")
    assert any(e[0] == "langfuse_fallback" for e in events)
    fallback_event = next(e for e in events if e[0] == "langfuse_fallback")
    assert "fetch-failed" in fallback_event[2]["reason"]


async def test_load_raises_if_neither_source_available(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    fake = _FakeLangfuse(exc=RuntimeError("down"))
    events: list[tuple[str, str, dict]] = []
    loader = PromptLoader(
        fake,
        local_dir=empty_dir,
        retries=0,
        observability_hook=lambda e, n, c: events.append((e, n, c)),
    )

    with pytest.raises(LoaderError):
        await loader.load("orchestrator")

    assert any(e[0] == "prompt_missing" for e in events), events


async def test_load_with_no_langfuse_client_reads_local_directly(prompt_dir: Path) -> None:
    loader = PromptLoader(None, local_dir=prompt_dir)
    result = await loader.load("orchestrator")

    assert result.source == PromptSource.LOCAL
    assert result.definition.name == "orchestrator"


async def test_cache_hit_does_not_call_langfuse_again(prompt_dir: Path) -> None:
    fake = _FakeLangfuse(payload=_langfuse_prompt("v1", version=1))
    loader = PromptLoader(fake, local_dir=prompt_dir, cache_ttl_seconds=60)

    first = await loader.load("orchestrator")
    second = await loader.load("orchestrator")

    assert first.source == PromptSource.LANGFUSE
    assert second.source == PromptSource.CACHE
    assert len(fake.calls) == 1


async def test_cache_expires_after_ttl(prompt_dir: Path) -> None:
    clock_value = 100.0

    def fake_clock() -> float:
        return clock_value

    fake = _FakeLangfuse(payload=_langfuse_prompt("v1", version=1))
    loader = PromptLoader(
        fake, local_dir=prompt_dir, cache_ttl_seconds=10, clock=fake_clock
    )

    first = await loader.load("orchestrator")
    assert first.source == PromptSource.LANGFUSE

    clock_value += 11.0
    second = await loader.load("orchestrator")

    assert second.source == PromptSource.LANGFUSE
    assert len(fake.calls) == 2


async def test_force_refresh_bypasses_cache(prompt_dir: Path) -> None:
    fake = _FakeLangfuse(payload=_langfuse_prompt("v1", version=1))
    loader = PromptLoader(fake, local_dir=prompt_dir, cache_ttl_seconds=300)

    await loader.load("orchestrator")
    await loader.load("orchestrator", force_refresh=True)

    assert len(fake.calls) == 2


async def test_invalid_yaml_raises_loader_error(tmp_path: Path) -> None:
    bad = tmp_path / "prompts" / "orchestrator"
    bad.mkdir(parents=True)
    (bad / "production.yaml").write_text("[1, 2, 3]\n")

    loader = PromptLoader(None, local_dir=tmp_path / "prompts")
    with pytest.raises(LoaderError):
        await loader.load("orchestrator")


async def test_compiled_prompt_render_substitutes_variables() -> None:
    defn = PromptDefinition(
        name="x",
        version=1,
        model="gemini-2.5-flash-lite",
        system="Hello {{who}} at {{tenant}}",
        user_template="Ask: {{question}}",
    )
    assert defn.format_system({"who": "Maya", "tenant": "Acme"}) == (
        "Hello Maya at Acme"
    )
    assert defn.format_user({"question": "status?"}) == "Ask: status?"


async def test_ships_orchestrator_production_yaml_in_repo() -> None:
    """Chunk 2.12 ships at least one prompt so ADR-0011 has a real artifact.

    The AICPA-language guard runs as a pre-commit hook on the YAML file
    itself, so this test just verifies shape and that the production
    fallback loads cleanly.
    """

    loader = PromptLoader(None)  # uses default_local_prompts_dir()
    compiled = await loader.load("orchestrator")
    assert compiled.source == PromptSource.LOCAL
    assert "AuditOrchestrator" in compiled.system
    assert compiled.definition.model.startswith("gemini")
    assert compiled.definition.guardrails.max_turns >= 1
