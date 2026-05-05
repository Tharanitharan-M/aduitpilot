"""Runtime prompt loader with Langfuse + local-YAML fallback.

Per ADR-0011: YAML in the repo is the source of truth and what Promptfoo
evals run against; Langfuse holds the same YAML, pushed on deploy,
swappable via the ``production`` label for sub-60-second hotfixes.

This module is intentionally independent of FastAPI / LangGraph so the
same loader can be used by the AdversarialAuditor service (Sprint 8) and
offline eval runners.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from apps.api.agents.prompts.schemas import PromptDefinition

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL_SECONDS = 60
DEFAULT_LANGFUSE_LABEL = "production"
DEFAULT_FETCH_TIMEOUT_SECONDS = 5.0
DEFAULT_RETRIES = 1


class PromptSource(str, Enum):
    """Where the loaded prompt was actually sourced from."""

    LANGFUSE = "langfuse"
    LOCAL = "local"
    CACHE = "cache"


@dataclasses.dataclass(frozen=True)
class CompiledPrompt:
    """The result of :meth:`PromptLoader.load` — the prompt plus provenance."""

    definition: PromptDefinition
    source: PromptSource
    loaded_at: float
    label: str = DEFAULT_LANGFUSE_LABEL

    @property
    def system(self) -> str:
        return self.definition.system


class LoaderError(RuntimeError):
    """Raised when neither Langfuse nor the local YAML can produce a prompt."""


def default_local_prompts_dir() -> Path:
    """Return the canonical ``apps/api/agents/prompts`` directory."""

    # __file__ = apps/api/agents/prompts/loader.py
    return Path(__file__).resolve().parent


@dataclass
class _CacheEntry:
    prompt: CompiledPrompt
    expires_at: float


class PromptLoader:
    """Fetch prompts from Langfuse with a YAML fallback.

    The loader is thread-safe-ish (one in-memory cache guarded by an
    asyncio lock) so a single shared instance across the FastAPI app is
    fine. Tests can construct per-test instances with ``cache_ttl=0``.

    Parameters
    ----------
    langfuse_client:
        Any object with ``.get_prompt(name, *, label)`` returning an
        object that has a ``.prompt`` string plus an optional ``.config``
        dict. The loader treats this very conservatively so Langfuse SDK
        changes do not cascade. Pass ``None`` to force local-only mode
        (useful in tests / offline dev).
    local_dir:
        Directory containing ``<name>/<label>.yaml`` files.
    observability_hook:
        Optional callback invoked with ``("langfuse_fallback" | "langfuse_hit" |
        "prompt_missing", name, context_dict)``. Tests use this to assert
        the operator would see a PostHog/Langfuse alert on fallback.
    """

    def __init__(
        self,
        langfuse_client: Any | None,
        *,
        local_dir: Path | None = None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        label: str = DEFAULT_LANGFUSE_LABEL,
        fetch_timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        observability_hook: Callable[[str, str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._langfuse = langfuse_client
        self._local_dir = local_dir or default_local_prompts_dir()
        self._cache_ttl = cache_ttl_seconds
        self._label = label
        self._timeout = fetch_timeout_seconds
        self._retries = retries
        self._hook = observability_hook
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    # ─── Public API ───────────────────────────────────────────────────────────
    async def load(self, name: str, *, force_refresh: bool = False) -> CompiledPrompt:
        async with self._lock:
            if not force_refresh:
                cached = self._cache.get(name)
                if cached and cached.expires_at > self._clock():
                    # Serve from cache; mark provenance so callers can log it.
                    return dataclasses.replace(cached.prompt, source=PromptSource.CACHE)

            prompt: CompiledPrompt
            try:
                prompt = await self._fetch_from_langfuse(name)
                self._emit("langfuse_hit", name, {"label": self._label})
            except _LangfuseUnavailable as exc:
                self._emit(
                    "langfuse_fallback",
                    name,
                    {"reason": exc.reason, "label": self._label},
                )
                prompt = self._load_local(name)

            self._cache[name] = _CacheEntry(
                prompt=prompt,
                expires_at=self._clock() + self._cache_ttl,
            )
            return prompt

    def invalidate(self, name: str | None = None) -> None:
        """Drop the cached entry for ``name`` (or every entry if ``None``)."""

        if name is None:
            self._cache.clear()
        else:
            self._cache.pop(name, None)

    # ─── Internals ────────────────────────────────────────────────────────────
    async def _fetch_from_langfuse(self, name: str) -> CompiledPrompt:
        if self._langfuse is None:
            raise _LangfuseUnavailable("no-client-configured")

        last_exc: BaseException | None = None
        for attempt in range(self._retries + 1):
            try:
                raw = await asyncio.wait_for(
                    self._call_langfuse(name),
                    timeout=self._timeout,
                )
            except TimeoutError as exc:
                last_exc = exc
                logger.warning(
                    "prompt.langfuse_timeout name=%s attempt=%d", name, attempt
                )
                continue
            except Exception as exc:  # noqa: BLE001 — SDK raises varied types
                last_exc = exc
                logger.warning(
                    "prompt.langfuse_error name=%s attempt=%d error=%s",
                    name,
                    attempt,
                    exc,
                )
                continue
            try:
                definition = _coerce_langfuse_payload(name, raw)
            except Exception as exc:  # noqa: BLE001
                raise _LangfuseUnavailable(f"invalid-payload: {exc}") from exc
            return CompiledPrompt(
                definition=definition,
                source=PromptSource.LANGFUSE,
                loaded_at=self._clock(),
                label=self._label,
            )

        raise _LangfuseUnavailable(f"fetch-failed: {last_exc!r}")

    async def _call_langfuse(self, name: str) -> Any:
        call = lambda: self._langfuse.get_prompt(name, label=self._label)  # noqa: E731
        return await asyncio.to_thread(call)

    def _load_local(self, name: str) -> CompiledPrompt:
        path = self._local_dir / name / f"{self._label}.yaml"
        if not path.exists():
            self._emit(
                "prompt_missing",
                name,
                {"path": str(path), "label": self._label},
            )
            raise LoaderError(
                f"prompt '{name}' not available from Langfuse and no local "
                f"fallback at {path}"
            )
        try:
            raw = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise LoaderError(f"local prompt '{name}' is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise LoaderError(
                f"local prompt '{name}' must be a YAML mapping, got {type(raw).__name__}"
            )
        definition = PromptDefinition.model_validate(raw)
        return CompiledPrompt(
            definition=definition,
            source=PromptSource.LOCAL,
            loaded_at=self._clock(),
            label=self._label,
        )

    def _emit(self, event: str, name: str, context: dict[str, Any]) -> None:
        if not self._hook:
            return
        try:
            self._hook(event, name, context)
        except Exception:  # noqa: BLE001 — observability must never crash the agent
            logger.exception("prompt.observability_hook_failed event=%s name=%s", event, name)


class _LangfuseUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _coerce_langfuse_payload(name: str, raw: Any) -> PromptDefinition:
    """Accept either a Langfuse ``TextPromptClient`` or a plain dict.

    Langfuse's client exposes ``.prompt`` (str) and ``.config`` (dict). We
    build a :class:`PromptDefinition` from those — every required field
    except ``system`` must arrive via ``.config``. If the Langfuse copy
    ever drifts from the YAML schema we fall back to local on the next
    cache miss (schema validation raises here).
    """

    if isinstance(raw, dict):
        return PromptDefinition.model_validate(raw)

    prompt_text = getattr(raw, "prompt", None)
    config = getattr(raw, "config", None)
    version = getattr(raw, "version", None)

    if prompt_text is None or not isinstance(config, dict):
        raise ValueError(
            f"Langfuse payload missing required .prompt / .config fields for '{name}'"
        )

    merged = {**config, "system": prompt_text, "name": config.get("name", name)}
    if version is not None:
        merged.setdefault("version", version)
    return PromptDefinition.model_validate(merged)
