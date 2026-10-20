"""Fact extraction, write triggering and candidate resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from domain.integrations import LLMPort
from domain.memory import (
    CompletedTurn,
    MemoryCandidate,
    MemoryFactExtractorPort,
    MemoryListRequest,
    MemoryRepositoryPort,
    MemoryResolution,
    MemoryResolverPort,
    MemoryWriteTriggerPort,
    ResolutionAction,
)

_PROMPT_ROOT = Path(__file__).resolve().parents[3] / "prompt"
_CATEGORIES = {
    "preference",
    "profile",
    "project",
    "technical_decision",
    "fact",
    "solution",
    "unresolved",
    "other",
}


def _response_text(value: Any) -> str:
    """Extract text from a LangChain response, mapping, or plain value."""
    if isinstance(value, dict):
        value = value.get("content", value.get("result", ""))
    else:
        value = getattr(value, "content", value)
    return str(value or "").strip()


def _json_object(value: Any) -> dict[str, Any]:
    """Decode JSON, including fenced responses emitted by chat models."""
    text = _response_text(value)
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            parsed = json.loads(text[start : end + 1])
        except (TypeError, ValueError):
            return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _fill(template: str, **values: str) -> str:
    """Fill only named placeholders; prompt examples contain literal JSON braces."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def _candidate_key(content: str) -> str:
    return content.split(":", 1)[0].strip().lower() if ":" in content else ""


class DefaultMemoryWriteTrigger(MemoryWriteTriggerPort):
    """Process every non-empty completed user turn."""

    def should_process(self, turn: CompletedTurn) -> bool:
        text = turn.user_text.strip()
        return bool(text) and not text.startswith("/") and len(text) <= 20_000


class LLMFactExtractor(MemoryFactExtractorPort):
    """Extract explicit, durable user facts with the configured secondary LLM."""

    def __init__(
        self,
        llm: LLMPort,
        memory: MemoryRepositoryPort | None = None,
        *,
        prompt_root: Path | None = None,
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._prompt_root = prompt_root or _PROMPT_ROOT

    async def extract(self, turn: CompletedTurn) -> list[MemoryCandidate]:
        existing = await self._existing_text(turn.session_id)
        prompt = _fill(
            (self._prompt_root / "fact_extraction.md").read_text(encoding="utf-8"),
            existing_memories=existing or "(无已有记忆)",
            conversation_text=(
                f"[用户] {turn.user_text}\n[助手] {turn.assistant_text}"
            ),
        )
        value = await self._llm.invoke(
            [
                {
                    "role": "system",
                    "content": "Extract durable user facts. Return JSON only.",
                },
                {"role": "user", "content": prompt},
            ]
        )
        payload = _json_object(value)
        facts = payload.get("atomic_facts", [])
        if not isinstance(facts, list):
            return []
        candidates: list[MemoryCandidate] = []
        for item in facts:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            fact = str(item.get("value", "")).strip()
            if not key or not fact:
                continue
            category = str(item.get("category", "other")).strip().lower()
            if category not in _CATEGORIES:
                category = "other"
            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < 0.6:
                continue
            candidates.append(
                MemoryCandidate(
                    content=f"{key}: {fact}"[:500],
                    source_turn_id=turn.turn_id,
                    memory_type=category,
                    category=category,
                    confidence=min(1.0, confidence),
                    session_id=turn.session_id,
                )
            )
        return candidates[:20]

    async def _existing_text(self, session_id: str) -> str:
        if self._memory is None or not session_id:
            return ""
        page = await self._memory.list_memories(
            MemoryListRequest(limit=50, session_id=session_id)
        )
        return "\n".join(
            f"- {item.content}"
            for item in page.items[:50]
            if item.status.value == "active"
        )


class LLMMemoryResolver(MemoryResolverPort):
    """Deduplicate candidates and decide CREATE, UPDATE or IGNORE."""

    def __init__(
        self,
        llm: LLMPort,
        memory: MemoryRepositoryPort,
        *,
        prompt_root: Path | None = None,
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._prompt_root = prompt_root or _PROMPT_ROOT

    async def resolve(
        self, candidates: list[MemoryCandidate]
    ) -> list[MemoryResolution]:
        resolutions: list[MemoryResolution] = []
        for candidate in candidates:
            existing = await self._existing(candidate)
            normalized = candidate.content.strip().casefold()
            exact = next(
                (
                    item
                    for item in existing
                    if item.content.strip().casefold() == normalized
                ),
                None,
            )
            if exact is not None:
                resolutions.append(
                    MemoryResolution(
                        ResolutionAction.IGNORE,
                        candidate,
                        target_memory_id=exact.id,
                        final_content=exact.content,
                    )
                )
                continue
            if not existing:
                resolutions.append(MemoryResolution(ResolutionAction.CREATE, candidate))
                continue
            action, target = await self._decide(candidate, existing)
            if action is ResolutionAction.IGNORE:
                resolutions.append(
                    MemoryResolution(
                        action,
                        candidate,
                        target_memory_id=target.id if target else None,
                        final_content=target.content if target else None,
                    )
                )
            elif action in {ResolutionAction.UPDATE, ResolutionAction.SUPERSEDE} and target:
                resolutions.append(
                    MemoryResolution(
                        action,
                        candidate,
                        target_memory_id=target.id,
                        final_content=candidate.content,
                    )
                )
            else:
                resolutions.append(MemoryResolution(ResolutionAction.CREATE, candidate))
        return resolutions

    async def _existing(self, candidate: MemoryCandidate) -> list[Any]:
        page = await self._memory.list_memories(
            MemoryListRequest(
                limit=50,
                session_id=candidate.session_id or None,
            )
        )
        return [item for item in page.items if item.status.value == "active"]

    async def _decide(
        self, candidate: MemoryCandidate, existing: list[Any]
    ) -> tuple[ResolutionAction, Any | None]:
        key = _candidate_key(candidate.content)
        same_key = [
            item for item in existing if key and _candidate_key(item.content) == key
        ]
        target = (same_key or existing)[0]
        prompt = _fill(
            (self._prompt_root / "memory_resolution.md").read_text(encoding="utf-8"),
            existing_memory="\n".join(
                f"- {item.id}: {item.content}" for item in existing
            ),
            candidate=candidate.content,
        )
        try:
            value = await self._llm.invoke(
                [
                    {
                        "role": "system",
                        "content": "Resolve one memory candidate. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ]
            )
            action = str(_json_object(value).get("action", "")).strip().upper()
        except Exception:
            action = ""
        if action == "IGNORE":
            return ResolutionAction.IGNORE, target
        if action == "UPDATE":
            return ResolutionAction.UPDATE, target
        if action == "SUPERSEDE":
            return ResolutionAction.SUPERSEDE, target
        if action == "CREATE":
            return ResolutionAction.CREATE, None
        # Do not lose an explicit fact because the resolver model is down.
        if same_key:
            return ResolutionAction.UPDATE, target
        return ResolutionAction.CREATE, None


__all__ = [
    "DefaultMemoryWriteTrigger",
    "LLMFactExtractor",
    "LLMMemoryResolver",
]
