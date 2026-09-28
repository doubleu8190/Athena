"""File artifacts, summaries, visual analysis, and code intelligence."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from athena.config.settings import Settings
from athena.core.files.adapter_registry import AdapterRegistry
from athena.core.files.attachment_serialization import attachment_to_payload
from athena.core.files.storage import StorageLayer
from athena.core.llm.provider import LLMProvider
from athena.infrastructure.postgre.repositories.file_repository import FileRepository
from athena.models.file import Attachment
from athena.utils.llm_response import extract_message_text


class FileAnalysisService:
    """Own artifact-backed analysis while leaving authorization to the facade."""

    def __init__(
        self,
        repository: FileRepository,
        *,
        settings: Settings,
        primary_llm: LLMProvider,
        secondary_llm: LLMProvider,
        adapter_registry: AdapterRegistry,
        storage: StorageLayer,
        cache_key,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.primary_llm = primary_llm
        self.secondary_llm = secondary_llm
        self.adapter_registry = adapter_registry
        self.storage = storage
        self.cache_key = cache_key

    async def extract_table(self, attachment: Attachment) -> dict[str, Any]:
        key = self.cache_key(
            attachment,
            "tables",
            {},
            attachment.adapter_version or "",
            self.settings.primary_llm.model,
        )
        artifact = await self.repository.get_artifact(key)
        return {"tables": json.loads(artifact.content) if artifact and artifact.content else []}

    async def summarize(
        self, attachment: Attachment, summary_type: str = "general"
    ) -> dict[str, Any]:
        key = self.cache_key(
            attachment,
            "summary",
            {"summary_type": summary_type},
            attachment.adapter_version or "",
            self.settings.primary_llm.model,
        )
        cached = await self.repository.get_artifact(key)
        if cached:
            return {"summary": cached.content or "", "cached": True}
        chunks = await self.repository.get_chunks(attachment.id, limit=100_000)
        if not chunks:
            if attachment.adapter_name == "image":
                message = (
                    "图片未识别出可总结的 OCR 文本。"
                    "如果需要描述截图画面，请使用 analyze_file；若未配置视觉模型，只能返回图片尺寸等元数据。"
                )
                await self.repository.put_artifact(
                    attachment.id, "summary", key, message, {"summary_type": summary_type}
                )
                return {"summary": message, "cached": False, "no_text": True}
            raise ValueError("文件尚未解析完成或没有可总结内容")
        summaries = [
            await self._llm_summary(
                self.secondary_llm,
                "\n\n".join(c.content for c in chunks[start : start + 8]),
                "分块摘要",
            )
            for start in range(0, len(chunks), 8)
        ]
        while len(summaries) > 8:
            summaries = [
                await self._llm_summary(
                    self.secondary_llm,
                    "\n\n".join(summaries[start : start + 8]),
                    "章节摘要",
                )
                for start in range(0, len(summaries), 8)
            ]
        final = await self._llm_summary(
            self.primary_llm, "\n\n".join(summaries), f"{summary_type} 文档摘要"
        )
        await self.repository.put_artifact(
            attachment.id, "summary", key, final, {"summary_type": summary_type}
        )
        return {"summary": final, "cached": False}

    async def analyze(self, attachment: Attachment, task: str) -> dict[str, Any]:
        adapter = self.adapter_registry.select(attachment.filename, attachment.mime_type)
        path = self.storage.resolve(attachment.storage_key)
        result = await adapter.analyze(path, task)
        if adapter.info.name == "image" and self.settings.primary_llm.supports_vision:
            result["vision"] = await self._vision_analysis(path, task)
        elif adapter.info.name == "image":
            result["can_describe_visual_content"] = False
            if str(result.get("ocr_text", "")).strip():
                result["analysis_source"] = "ocr"
                result["message"] = (
                    "当前主模型未声明支持视觉输入，因此无法描述图片画面；"
                    "本次使用 OCR 识别出的文字作为 fallback，仅能分析图片中的可识别文本。"
                )
            else:
                result["message"] = (
                    "当前主模型未声明支持视觉输入，因此无法描述图片画面；"
                    "图片也未识别出可用的 OCR 文本，只能返回尺寸、模式和 OCR 可用性。"
                )
        return result

    @staticmethod
    def codebase_overview(attachment: Attachment) -> dict[str, Any]:
        return {
            "file": attachment_to_payload(attachment),
            "languages": attachment.metadata.languages,
            "files": attachment.metadata.files or 1,
            "symbols": attachment.metadata.symbol_count or 0,
            "dependencies": attachment.metadata.dependency_count or 0,
        }

    async def find_symbol(self, file_id: str, name: str) -> list[dict[str, Any]]:
        return await self.repository.find_symbols(file_id, name)

    async def call_graph(
        self, file_id: str, symbol: str, direction: str
    ) -> list[dict[str, Any]]:
        return await self.repository.find_dependencies(file_id, symbol, direction)

    async def _llm_summary(self, provider: LLMProvider, content: str, label: str) -> str:
        response = await provider.ainvoke(
            [
                HumanMessage(
                    content=f"请生成忠实、紧凑的{label}。保留事实、数字、风险和结论，不添加原文没有的信息。\n\n{content}"
                )
            ]
        )
        return extract_message_text(response).strip()

    async def _vision_analysis(self, path: Path, task: str) -> str:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        response = await self.primary_llm.ainvoke(
            [
                HumanMessage(
                    content=[
                        {"type": "text", "text": task or "请描述并分析这张图片。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ]
                )
            ]
        )
        return extract_message_text(response)


__all__ = ["FileAnalysisService"]
