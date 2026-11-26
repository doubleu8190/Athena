"""M3.2 后端验收测试：多模态视觉输入、图片隐私剥离、预算隔离与非识图温柔降级。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import random
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from access_support import make_test_access_policy
from openai import AsyncOpenAI
from PIL import Image, PngImagePlugin

from adapter.file.storage import FileStorageError
from adapter.llm.openai_chat_completions import OpenAIChatCompletionsAdapter
from app.file.service import SessionFileService
from app.run.compression import ContextCompressor
from app.run.config import RunWorkerConfig
from app.run.context import (
    LlmRequestSpec,
    estimate_vision_image_tokens,
    measure_llm_request,
    messages_to_llm,
)
from app.run.executor import RunExecutor
from app.tool.ports import RunToolRuntime
from app.tool.runtime import REQUEST_USER_INPUT_DEFINITION, RunToolRuntimeService
from bootstrap.api import build_api_dependencies
from bootstrap.run_worker import RunWorkerBundle, build_run_worker_dependencies
from domain.file import (
    MAX_VISION_ENCODED_IMAGE_BYTES,
    MAX_VISION_IMAGE_EDGE,
    MAX_VISION_IMAGE_PIXELS,
    MAX_VISION_IMAGES_PER_RUN,
    MAX_VISION_RAW_IMAGE_BYTES,
    VISION_IMAGE_MIME_TYPES,
    FileObjectStore,
    NormalizedVisionImage,
    SessionFile,
    SessionFileRepository,
    SessionFileSource,
    VisionImageError,
)
from domain.llm.model import (
    CompressionPolicyVersion,
    LlmCapabilityError,
    LlmImagePart,
    LlmMessage,
    LlmProtocolError,
    LlmRequest,
    LlmResponse,
    LlmRole,
    LlmStreamEvent,
    LlmText,
    LlmTextPart,
    LlmToolCall,
    LlmUsage,
    ModelProfileVersion,
    ResponseCompleted,
    RuntimeModelProfile,
    TextDelta,
)
from domain.llm.ports import LlmRuntimeTextProvider, LlmStreamProvider
from domain.llm.profile_ports import ModelProfileRepository
from domain.message import (
    ConversationMessage,
    ConversationRole,
    ExecutionMessage,
    ExecutionRole,
    FileRefPart,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolCallPart,
)
from domain.run.model import (
    ContextStopReason,
    ContextSummary,
    FailRunCommand,
    FinishRunCommand,
    RecordRunUsageCommand,
    Run,
    RunEvent,
    RunInputFileContext,
    RunInputFiles,
    RunInputImageAttachment,
    RunLease,
    RunModelSnapshot,
    RunStatus,
    RunUsage,
    SaveContextSummaryCommand,
)
from domain.run.ports import RunRepository
from domain.system_prompt import SystemPromptKind, SystemPromptVersion
from domain.tool import (
    BeginToolCallBatchCommand,
    BeginToolCallBatchResult,
    BuiltinToolSource,
    FinishToolCallCommand,
    InitializeRunToolsCommand,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    RunToolBatch,
    RunToolBatchCall,
    RunToolCallRecord,
    ToolCallStatus,
    ToolExecutionResult,
    ToolOutcome,
    ToolOutcomeStatus,
)
from infrastructure.file import LocalVisionImageNormalizer

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 测试图片与领域对象构造辅助
# ---------------------------------------------------------------------------


def _make_directional_image(
    width: int,
    height: int,
    *,
    mode: str = "RGB",
) -> Image.Image:
    """生成左右/上下颜色可区分的图像，用于验证 EXIF 方向旋转是否真正生效。"""
    color_bg = (20, 40, 200, 128) if mode == "RGBA" else (20, 40, 200)
    color_top = (230, 30, 30, 255) if mode == "RGBA" else (230, 30, 30)
    image = Image.new(mode, (width, height), color_bg)
    band_height = max(1, height // 4)
    # 顶部横条为红色；当 EXIF Orientation=6（顺时针旋转 90°）生效后，红色带会移动到最右侧。
    for y in range(band_height):
        for x in range(width):
            image.putpixel((x, y), color_top)
    return image


def _encode_jpeg_with_privacy_metadata(width: int, height: int, *, orientation: int = 6) -> bytes:
    """生成含 EXIF 方向、相机型号、GPS 坐标与 ICC Profile 的真实 JPEG。"""
    image = _make_directional_image(width, height, mode="RGB")
    try:
        exif = image.getexif()
        exif[274] = orientation
        exif[271] = "SecretCameraMake-M32"
        exif[272] = "SecretCameraModel-M32"
        exif[34853] = {
            1: "N",
            2: (39.0, 54.0, 15.0),
            3: "E",
            4: (116.0, 24.0, 27.0),
        }
        buf = io.BytesIO()
        image.save(
            buf,
            format="JPEG",
            quality=90,
            exif=exif.tobytes(),
            icc_profile=b"SECRET-ICC-PROFILE-PAYLOAD",
        )
        return buf.getvalue()
    finally:
        image.close()


def _encode_png_with_privacy_metadata(
    width: int,
    height: int,
    *,
    orientation: int = 8,
    alpha: bool = True,
) -> bytes:
    """生成含透明通道、EXIF、XMP、ICC 与 PNG tEXt 块的真实 PNG。"""
    image = _make_directional_image(width, height, mode="RGBA" if alpha else "RGB")
    try:
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("Author", "SecretPngAuthor-M32")
        pnginfo.add_text("XML:com.adobe.xmp", "<x:xmpmeta>SecretPngXMP-M32</x:xmpmeta>")
        exif = image.getexif()
        exif[274] = orientation
        exif[271] = "SecretPngCamera-M32"
        buf = io.BytesIO()
        image.save(
            buf,
            format="PNG",
            pnginfo=pnginfo,
            exif=exif.tobytes(),
            icc_profile=b"SECRET-PNG-ICC-PROFILE",
        )
        return buf.getvalue()
    finally:
        image.close()


def _encode_webp_with_privacy_metadata(width: int, height: int, *, orientation: int = 6) -> bytes:
    """生成含 EXIF、XMP、ICC 与透明通道的真实 WEBP。"""
    image = _make_directional_image(width, height, mode="RGBA")
    try:
        exif = image.getexif()
        exif[274] = orientation
        exif[271] = "SecretWebpCamera-M32"
        buf = io.BytesIO()
        image.save(
            buf,
            format="WEBP",
            quality=85,
            exif=exif.tobytes(),
            xmp=b"<x:xmpmeta>SecretWebpXMP-M32</x:xmpmeta>",
            icc_profile=b"SECRET-WEBP-ICC-PROFILE",
        )
        return buf.getvalue()
    finally:
        image.close()


def _encode_noisy_image(width: int, height: int, *, image_format: str, mode: str = "RGBA") -> bytes:
    """生成高熵随机噪点图片，用于触发体积收敛降级与 4 MiB 上限边界测试。"""
    rng = random.Random(42)
    channels = 4 if mode == "RGBA" else 3
    raw = bytes(rng.getrandbits(8) for _ in range(width * height * channels))
    image = Image.frombytes(mode, (width, height), raw)
    try:
        buf = io.BytesIO()
        image.save(buf, format=image_format)
        return buf.getvalue()
    finally:
        image.close()


def _runtime_profile(
    *,
    profile_id: str = "mpv_vision",
    supports_vision: bool = True,
    context_window_tokens: int = 8192,
    max_output_tokens: int = 512,
) -> RuntimeModelProfile:
    return RuntimeModelProfile(
        version=ModelProfileVersion(
            id=profile_id,
            name="vision-test",
            protocol="chat_completions",
            base_url="https://llm.example/v1",
            model="gpt-4o-test",
            context_window_tokens=context_window_tokens,
            tokenizer_encoding="o200k_base",
            supports_stream=True,
            supports_tools=True,
            max_output_tokens=max_output_tokens,
            request_timeout_seconds=30,
            execution_timeout_seconds=60,
            max_concurrency=2,
            temperature=None,
            enabled=True,
            created_at=NOW,
            updated_at=NOW,
            supports_vision=supports_vision,
        ),
        api_key="secret-api-key",
    )


def _sample_run(
    *,
    run_id: str = "run_m32",
    model_profile_id: str = "mpv_vision",
    input_file_ids: tuple[str, ...] = (),
    compression_policy_version_id: str | None = None,
    tool_snapshot: RuntimeToolSnapshot | None = None,
    trigger_message_id: str = "msg_trigger",
) -> Run:
    return Run(
        id=run_id,
        session_id="ses_m32",
        owner_id="usr_m32",
        status=RunStatus.RUNNING,
        trigger_message_id=trigger_message_id,
        context_cutoff_sequence=10,
        retry_of_run_id=None,
        model_config_version_id=model_profile_id,
        model_snapshot=RunModelSnapshot(
            id=model_profile_id,
            name="vision-test",
            protocol="chat_completions",
            base_url="https://llm.example/v1",
            model="gpt-4o-test",
            context_window_tokens=8192,
            tokenizer_encoding="o200k_base",
            supports_stream=True,
            supports_tools=True,
            supports_parallel_tool_calls=False,
            max_output_tokens=512,
            request_timeout_seconds=30,
            execution_timeout_seconds=60,
            max_concurrency=2,
            supports_vision=True,
        ),
        main_prompt_version_id="spv_main",
        compression_prompt_version_id="spv_cmp",
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker_m32",
        lease_expires_at=NOW + timedelta(seconds=60),
        execution_started_at=NOW,
        execution_deadline_at=NOW + timedelta(seconds=60),
        queued_at=NOW,
        first_started_at=NOW,
        finished_at=None,
        created_at=NOW,
        updated_at=NOW,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
        compression_policy_version_id=compression_policy_version_id,
        tool_snapshot=tool_snapshot,
        auth_session_id="auth_m32",
        input_files=RunInputFiles(file_ids=input_file_ids),
    )


def _session_file(
    file_id: str,
    filename: str,
    mime_type: str,
    raw_bytes: bytes,
    *,
    size_bytes_override: int | None = None,
) -> SessionFile:
    digest = hashlib.sha256(raw_bytes).hexdigest()
    return SessionFile(
        id=file_id,
        session_id="ses_m32",
        owner_id="usr_m32",
        source=SessionFileSource.UPLOAD,
        filename=filename,
        label=None,
        mime_type=mime_type,
        size_bytes=len(raw_bytes) if size_bytes_override is None else size_bytes_override,
        sha256=digest,
        object_key=f"session-files/{digest[:2]}/{file_id}",
        source_call_id=None,
        output_index=None,
        created_at=NOW,
    )


class _InMemoryObjectStore:
    """记录读取次数与上限的内存对象存储替身。"""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = dict(blobs)
        self.get_bytes_calls: list[tuple[str, int]] = []
        self.raise_on_key: dict[str, Exception] = {}

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        self.get_bytes_calls.append((key, maximum_bytes))
        if key in self.raise_on_key:
            raise self.raise_on_key[key]
        data = self._blobs[key]
        if len(data) > maximum_bytes:
            raise FileStorageError("file_storage_object_too_large")
        return data


class _InMemorySessionFileRepo:
    """按会话隔离返回已授权文件元数据的内存仓储替身。"""

    def __init__(self, files: Sequence[SessionFile]) -> None:
        self._by_id = {item.id: item for item in files}

    async def get_visible_many(
        self,
        session_id: str,
        owner_id: str,
        file_ids: tuple[str, ...],
    ) -> tuple[SessionFile, ...]:
        result: list[SessionFile] = []
        for file_id in file_ids:
            item = self._by_id.get(file_id)
            if item is None or item.session_id != session_id or item.owner_id != owner_id:
                raise ValueError("session_file_not_found")
            result.append(item)
        return tuple(result)


# ---------------------------------------------------------------------------
# 一、图片安全归一化与 EXIF/GPS 隐私剥离测试
# ---------------------------------------------------------------------------


def test_normalizer_strips_exif_gps_icc_transposes_and_resizes_jpeg() -> None:
    """超大带 EXIF 旋转与 GPS/ICC 的 JPEG 归一化后长边 <=2048、方向正确且元数据 100% 剥离。"""
    raw = _encode_jpeg_with_privacy_metadata(3000, 1500, orientation=6)
    normalizer = LocalVisionImageNormalizer()

    result = asyncio.run(
        normalizer.normalize(
            file_id="file_jpg",
            filename="photo.jpg",
            declared_mime_type="image/jpeg",
            raw_bytes=raw,
        )
    )

    assert result.mime_type == "image/jpeg"
    # 3000x1500 顺时针旋转 90° 后为 1500x3000，按 2048 长边等比缩放为 1024x2048。
    assert result.width == 1024
    assert result.height == 2048
    assert max(result.width, result.height) <= MAX_VISION_IMAGE_EDGE
    assert result.size_bytes <= MAX_VISION_ENCODED_IMAGE_BYTES
    # base64 载荷绝不进入 repr。
    assert result.data_base64[:24] not in repr(result)

    decoded = base64.b64decode(result.data_base64)
    assert len(decoded) == result.size_bytes
    for secret in (b"SecretCameraMake", b"SecretCameraModel", b"SECRET-ICC-PROFILE"):
        assert secret not in decoded
    with Image.open(io.BytesIO(decoded)) as reloaded:
        assert reloaded.format == "JPEG"
        assert reloaded.size == (1024, 2048)
        assert len(reloaded.getexif()) == 0
        assert "exif" not in reloaded.info
        assert "icc_profile" not in reloaded.info
        # 顺时针旋转 90° 后，原顶部红带应位于图像右侧。
        right_pixel = cast(tuple[int, int, int], reloaded.getpixel((reloaded.width - 10, 500)))
        left_pixel = cast(tuple[int, int, int], reloaded.getpixel((10, 500)))
        assert right_pixel[0] > 180 and right_pixel[2] < 80
        assert left_pixel[2] > 150 and left_pixel[0] < 80


def test_normalizer_preserves_png_alpha_and_strips_xmp_text_exif_and_webp_metadata() -> None:
    """PNG 保留透明通道且剥离全部 EXIF/XMP/ICC/tEXt；WEBP 超大缩放并剥离全部隐私元数据。"""
    normalizer = LocalVisionImageNormalizer()

    raw_png = _encode_png_with_privacy_metadata(2400, 1200, orientation=8, alpha=True)
    png_result = asyncio.run(
        normalizer.normalize(
            file_id="file_png",
            filename="ui.png",
            declared_mime_type="image/png",
            raw_bytes=raw_png,
        )
    )
    assert png_result.mime_type == "image/png"
    assert png_result.width == 1024
    assert png_result.height == 2048
    png_bytes = base64.b64decode(png_result.data_base64)
    for secret in (b"SecretPngAuthor", b"SecretPngXMP", b"SecretPngCamera", b"SECRET-PNG-ICC"):
        assert secret not in png_bytes
    with Image.open(io.BytesIO(png_bytes)) as reloaded_png:
        assert reloaded_png.format == "PNG"
        assert reloaded_png.mode == "RGBA"
        assert len(reloaded_png.getexif()) == 0
        assert reloaded_png.info == {}
        # 验证透明通道未丢失（原蓝色区域 alpha == 128）。
        sample_rgba = cast(
            tuple[int, int, int, int],
            reloaded_png.getpixel((reloaded_png.width - 10, 500)),
        )
        assert sample_rgba[3] < 200

    raw_webp = _encode_webp_with_privacy_metadata(2600, 1300, orientation=6)
    webp_result = asyncio.run(
        normalizer.normalize(
            file_id="file_webp",
            filename="banner.webp",
            declared_mime_type="image/webp",
            raw_bytes=raw_webp,
        )
    )
    assert webp_result.mime_type == "image/webp"
    assert max(webp_result.width, webp_result.height) <= MAX_VISION_IMAGE_EDGE
    webp_bytes = base64.b64decode(webp_result.data_base64)
    for secret in (b"SecretWebpXMP", b"SecretWebpCamera", b"SECRET-WEBP-ICC"):
        assert secret not in webp_bytes
    with Image.open(io.BytesIO(webp_bytes)) as reloaded_webp:
        assert reloaded_webp.format == "WEBP"
        assert len(reloaded_webp.getexif()) == 0
        assert "exif" not in reloaded_webp.info
        assert "xmp" not in reloaded_webp.info
        assert "icc_profile" not in reloaded_webp.info


def test_normalizer_png_fallback_to_lossy_and_rejects_oversized_corrupt_bomb_and_non_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 PNG 超限安全降级、质量阶梯收敛、不可收敛报错、损坏图片、解压炸弹与非白名单格式。"""
    # 1. 高熵噪点 PNG 超过受限编码预算时，自动转白底 RGB 并降级为 WEBP/JPEG 收敛。
    noisy_png = _encode_noisy_image(256, 256, image_format="PNG", mode="RGBA")
    assert len(noisy_png) > 80_000
    tight_normalizer = LocalVisionImageNormalizer(max_encoded_bytes=60_000)
    degraded = asyncio.run(
        tight_normalizer.normalize(
            file_id="file_noisy",
            filename="noisy.png",
            declared_mime_type="image/png",
            raw_bytes=noisy_png,
        )
    )
    assert degraded.mime_type in {"image/webp", "image/jpeg"}
    assert degraded.size_bytes <= 60_000

    # 1b. 高熵噪点 JPEG / WEBP 超限时按质量阶梯与温和缩边收敛。
    noisy_jpg = _encode_noisy_image(300, 300, image_format="JPEG", mode="RGB")
    jpg_converged = asyncio.run(
        LocalVisionImageNormalizer(max_encoded_bytes=max(15_000, len(noisy_jpg) // 2)).normalize(
            file_id="file_noisy_jpg",
            filename="noisy.jpg",
            declared_mime_type="image/jpeg",
            raw_bytes=noisy_jpg,
        )
    )
    assert jpg_converged.mime_type == "image/jpeg"
    assert jpg_converged.size_bytes <= max(15_000, len(noisy_jpg) // 2)

    noisy_webp = _encode_noisy_image(300, 300, image_format="WEBP", mode="RGBA")
    webp_converged = asyncio.run(
        LocalVisionImageNormalizer(max_encoded_bytes=max(20_000, len(noisy_webp) // 2)).normalize(
            file_id="file_noisy_webp",
            filename="noisy.webp",
            declared_mime_type="image/webp",
            raw_bytes=noisy_webp,
        )
    )
    assert webp_converged.mime_type == "image/webp"
    assert webp_converged.size_bytes <= max(20_000, len(noisy_webp) // 2)

    # 2. 即使完成质量阶梯与适度缩边仍超限 -> 抛出 vision_image_too_large。
    impossible_normalizer = LocalVisionImageNormalizer(max_encoded_bytes=64)
    with pytest.raises(VisionImageError) as too_large_err:
        asyncio.run(
            impossible_normalizer.normalize(
                file_id="file_impossible",
                filename="noisy.png",
                declared_mime_type="image/png",
                raw_bytes=noisy_png,
            )
        )
    assert too_large_err.value.code == "vision_image_too_large"

    # 3. 原始字节超限 -> vision_image_too_large。
    tiny_raw_normalizer = LocalVisionImageNormalizer(max_raw_bytes=100)
    with pytest.raises(VisionImageError) as raw_too_large_err:
        asyncio.run(
            tiny_raw_normalizer.normalize(
                file_id="file_raw_big",
                filename="photo.jpg",
                declared_mime_type="image/jpeg",
                raw_bytes=b"\xff\xd8\xff" + b"0" * 200,
            )
        )
    assert raw_too_large_err.value.code == "vision_image_too_large"

    # 4. 损坏字节与空字节 -> vision_image_invalid。
    normalizer = LocalVisionImageNormalizer()
    for bad_bytes in (b"", b"not-a-real-image-payload"):
        with pytest.raises(VisionImageError) as invalid_err:
            asyncio.run(
                normalizer.normalize(
                    file_id="file_bad",
                    filename="broken.png",
                    declared_mime_type="image/png",
                    raw_bytes=bad_bytes,
                )
            )
        assert invalid_err.value.code == "vision_image_invalid"

    # 5. 解压炸弹像素超限（不解码像素即拦截）与 Pillow DecompressionBombError 映射。
    low_pixel_normalizer = LocalVisionImageNormalizer(max_pixels=10_000)
    small_png = _encode_png_with_privacy_metadata(200, 200, orientation=1, alpha=False)
    with pytest.raises(VisionImageError) as bomb_err:
        asyncio.run(
            low_pixel_normalizer.normalize(
                file_id="file_bomb",
                filename="bomb.png",
                declared_mime_type="image/png",
                raw_bytes=small_png,
            )
        )
    assert bomb_err.value.code == "vision_image_too_large"
    assert MAX_VISION_IMAGE_PIXELS == 40_000_000

    def _raise_bomb(*_args: Any, **_kwargs: Any) -> Any:
        raise Image.DecompressionBombError("simulated bomb")

    monkeypatch.setattr(Image, "open", _raise_bomb)
    with pytest.raises(VisionImageError) as pil_bomb_err:
        asyncio.run(
            normalizer.normalize(
                file_id="file_bomb_pil",
                filename="bomb2.png",
                declared_mime_type="image/png",
                raw_bytes=small_png,
            )
        )
    assert pil_bomb_err.value.code == "vision_image_too_large"
    monkeypatch.undo()

    # 6. 非白名单格式（真实 GIF 伪装成 PNG，以及声明 PDF）-> vision_image_unsupported。
    gif_buf = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 255, 0)).save(gif_buf, format="GIF")
    with pytest.raises(VisionImageError) as gif_err:
        asyncio.run(
            normalizer.normalize(
                file_id="file_gif",
                filename="anim.png",
                declared_mime_type="image/png",
                raw_bytes=gif_buf.getvalue(),
            )
        )
    assert gif_err.value.code == "vision_image_unsupported"

    with pytest.raises(VisionImageError) as pdf_err:
        asyncio.run(
            normalizer.normalize(
                file_id="file_pdf",
                filename="doc.pdf",
                declared_mime_type="application/pdf",
                raw_bytes=b"%PDF-1.7",
            )
        )
    assert pdf_err.value.code == "vision_image_unsupported"


def test_session_file_service_load_vision_image_enforces_20mib_limit_and_error_mapping() -> None:
    """验证 SessionFileService.load_vision_image 严格传递 20 MiB 上限并映射安全错误码。"""
    valid_png = _encode_png_with_privacy_metadata(64, 64, orientation=1, alpha=False)
    f_ok = _session_file("file_ok", "ok.png", "image/png", valid_png)
    f_big_meta = _session_file(
        "file_big_meta",
        "big.png",
        "image/png",
        valid_png,
        size_bytes_override=MAX_VISION_RAW_IMAGE_BYTES + 1,
    )
    f_storage_err = _session_file("file_err", "err.png", "image/png", valid_png)
    f_text = _session_file("file_txt", "a.txt", "text/plain", b"hello")

    store = _InMemoryObjectStore(
        {
            f_ok.object_key: valid_png,
            f_big_meta.object_key: valid_png,
            f_storage_err.object_key: valid_png,
        }
    )
    store.raise_on_key[f_storage_err.object_key] = FileStorageError("file_storage_object_too_large")
    repo = _InMemorySessionFileRepo((f_ok, f_big_meta, f_storage_err, f_text))
    service = SessionFileService(
        cast(SessionFileRepository, repo),
        cast(FileObjectStore, store),
        image_normalizer=LocalVisionImageNormalizer(),
    )

    loaded = asyncio.run(service.load_vision_image(f_ok))
    assert loaded.file_id == "file_ok"
    assert loaded.mime_type == "image/png"
    assert store.get_bytes_calls == [(f_ok.object_key, MAX_VISION_RAW_IMAGE_BYTES)]

    with pytest.raises(VisionImageError) as err_meta:
        asyncio.run(service.load_vision_image(f_big_meta))
    assert err_meta.value.code == "vision_image_too_large"
    # 元数据超限在读取对象存储前即被拦截。
    assert len(store.get_bytes_calls) == 1

    with pytest.raises(VisionImageError) as err_storage:
        asyncio.run(service.load_vision_image(f_storage_err))
    assert err_storage.value.code == "vision_image_too_large"

    with pytest.raises(VisionImageError) as err_txt:
        asyncio.run(service.load_vision_image(f_text))
    assert err_txt.value.code == "vision_image_unsupported"


# ---------------------------------------------------------------------------
# 二、OpenAI Chat Completions 视觉载荷序列化与门禁测试
# ---------------------------------------------------------------------------


def test_openai_adapter_serializes_multimodal_user_message_and_enforces_gates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证 OpenAI 视觉数组格式、supports_vision 门禁、非法角色拦截与 complete 纯文本隔离。"""
    secret_b64 = base64.b64encode(b"SECRET-VISION-IMAGE-BYTES").decode("ascii")
    image_part = LlmImagePart(
        mime_type="image/png",
        data_base64=secret_b64,
        detail="high",
        width=512,
        height=512,
    )
    assert secret_b64 not in repr(image_part)

    # 1. 标准多模态 USER 消息与纯文本消息混合序列化。
    messages = (
        LlmMessage(LlmRole.SYSTEM, "你是助手"),
        LlmMessage(
            LlmRole.USER,
            content="请分析下图：",
            content_parts=(LlmTextPart("图片说明"), image_part),
        ),
        LlmMessage(LlmRole.USER, content="", content_parts=(LlmTextPart(""),)),
    )
    serialized = OpenAIChatCompletionsAdapter._messages(messages)
    assert serialized[0] == {"role": "system", "content": "你是助手"}
    assert serialized[1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "请分析下图："},
            {"type": "text", "text": "图片说明"},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{secret_b64}",
                    "detail": "high",
                },
            },
        ],
    }
    # 空文本部件回退为空字符串。
    assert serialized[2] == {"role": "user", "content": ""}

    # 2. SYSTEM / ASSISTANT / TOOL 携带图片一律拒绝。
    for bad_msg in (
        LlmMessage(LlmRole.SYSTEM, content_parts=(image_part,)),
        LlmMessage(LlmRole.ASSISTANT, content_parts=(image_part,)),
        LlmMessage(
            LlmRole.ASSISTANT,
            tool_calls=(LlmToolCall("c1", "tool_a", {}),),
            content_parts=(image_part,),
        ),
        LlmMessage(LlmRole.TOOL, tool_call_id="c1", content_parts=(image_part,)),
    ):
        with pytest.raises(LlmProtocolError) as role_err:
            OpenAIChatCompletionsAdapter._messages((bad_msg,))
        assert role_err.value.code == "llm_chat_image_role_invalid"
        assert secret_b64 not in str(role_err.value)

    # 3. 非法 MIME / 空 base64 / 非法 detail 拒绝且不泄露 base64。
    for bad_part in (
        LlmImagePart(mime_type=cast(Any, "image/gif"), data_base64=secret_b64),
        LlmImagePart(mime_type="image/png", data_base64=""),
        LlmImagePart(mime_type="image/png", data_base64="   "),
        LlmImagePart(mime_type="image/png", data_base64="abc def"),
        LlmImagePart(
            mime_type="image/png",
            data_base64=secret_b64,
            detail=cast(Any, "ultra"),
        ),
    ):
        with pytest.raises(LlmProtocolError) as invalid_part_err:
            OpenAIChatCompletionsAdapter._messages(
                (LlmMessage(LlmRole.USER, content_parts=(bad_part,)),)
            )
        assert invalid_part_err.value.code == "llm_chat_image_invalid"
        assert secret_b64 not in str(invalid_part_err.value)

    # 4. stream() 对 supports_vision=False 抛出 LlmCapabilityError("llm_vision_unsupported")。
    adapter = OpenAIChatCompletionsAdapter()
    captured_payload: dict[str, Any] = {}

    class _FakeClosableStream:
        async def __aiter__(self) -> AsyncIterator[object]:
            delta = SimpleNamespace(content="看到一张图", tool_calls=None)
            choice = SimpleNamespace(index=0, delta=delta, finish_reason="stop")
            yield SimpleNamespace(
                id="chatcmpl_vision",
                choices=[choice],
                usage=SimpleNamespace(prompt_tokens=300, completion_tokens=10, total_tokens=310),
            )

        async def close(self) -> None:
            return None

    class _FakeCreate:
        async def __call__(self, **kwargs: Any) -> _FakeClosableStream:
            captured_payload.update(kwargs)
            return _FakeClosableStream()

    adapter._client = lambda runtime: cast(  # type: ignore[method-assign]
        AsyncOpenAI,
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_FakeCreate()))),
    )

    vision_request = LlmRequest(
        model_profile="mpv_vision",
        messages=(LlmMessage(LlmRole.USER, content_parts=(LlmTextPart("看图"), image_part)),),
    )
    assert vision_request.has_image_parts()

    async def _try_non_vision_stream() -> None:
        async with adapter.stream(_runtime_profile(supports_vision=False), vision_request):
            pass

    with pytest.raises(LlmCapabilityError) as cap_err:
        asyncio.run(_try_non_vision_stream())
    assert cap_err.value.code == "llm_vision_unsupported"
    assert captured_payload == {}

    # 5. stream() 对 supports_vision=True 成功发送多模态 payload，且日志不含 base64。
    async def _run_vision_stream() -> list[LlmStreamEvent]:
        async with adapter.stream(_runtime_profile(supports_vision=True), vision_request) as events:
            return [event async for event in events]

    with caplog.at_level("DEBUG"):
        events = asyncio.run(_run_vision_stream())
    assert any(isinstance(e, ResponseCompleted) for e in events)
    sent_messages = cast(list[dict[str, Any]], captured_payload["messages"])
    assert sent_messages[0]["role"] == "user"
    assert (
        sent_messages[0]["content"][1]["image_url"]["url"] == f"data:image/png;base64,{secret_b64}"
    )
    assert secret_b64 not in caplog.text

    # 6. complete()（摘要压缩 / 自动标题 / 安全停止报告纯文本路径）即使支持识图也严禁图片。
    complete_request = LlmRequest(
        model_profile="mpv_vision",
        messages=(LlmMessage(LlmRole.USER, content_parts=(image_part,)),),
        tool_choice="none",
    )
    with pytest.raises(LlmProtocolError) as cmp_err:
        asyncio.run(adapter.complete(_runtime_profile(supports_vision=True), complete_request))
    assert cmp_err.value.code == "llm_compression_image_unsupported"


# ---------------------------------------------------------------------------
# 三、视觉 Token 预算估算与压缩隔离测试
# ---------------------------------------------------------------------------


def test_vision_token_estimation_and_base64_isolation_in_measure_llm_request() -> None:
    """验证视觉分块公式数值准确，且 100KB+ base64 绝不被送入 tiktoken 造成虚高 Token。"""
    assert estimate_vision_image_tokens(4096, 4096, "low") == 85
    assert estimate_vision_image_tokens(None, None, "low") == 85
    assert estimate_vision_image_tokens(None, None, "auto") == 1105
    assert estimate_vision_image_tokens(512, 512, "auto") == 255
    assert estimate_vision_image_tokens(1024, 1024, "auto") == 765
    assert estimate_vision_image_tokens(2048, 4096, "high") == 1105

    run = _sample_run()
    profile = _runtime_profile(supports_vision=True).version
    huge_b64 = base64.b64encode(b"A" * 120_000).decode("ascii")
    assert len(huge_b64) == 160_000
    attachment = RunInputImageAttachment(
        file_id="file_img_1",
        filename="dashboard.png",
        mime_type="image/png",
        data_base64=huge_b64,
        width=1024,
        height=1024,
    )
    assert huge_b64[:32] not in repr(attachment)

    file_ctx = RunInputFileContext(
        file_id="file_img_1",
        filename="dashboard.png",
        label=None,
        mime_type="image/png",
        size_bytes=120_000,
        source="upload",
        vision_status="attached",
    )

    _, _, tokens_without_img, _ = asyncio.run(
        measure_llm_request(
            LlmRequestSpec(
                run=run,
                messages=(),
                main_prompt="系统提示",
                input_files=(file_ctx,),
            ),
            profile,
            timeout_seconds=10.0,
        )
    )
    llm_messages, _, tokens_with_img, _ = asyncio.run(
        measure_llm_request(
            LlmRequestSpec(
                run=run,
                messages=(),
                main_prompt="系统提示",
                input_files=(file_ctx,),
                input_images=(attachment,),
            ),
            profile,
            timeout_seconds=10.0,
        )
    )

    # 1024x1024 视觉分块约 765 tokens，加上边界说明与标签后仍远低于 base64 假文本量级（数万）。
    delta = tokens_with_img - tokens_without_img
    assert 765 <= delta <= 1_050

    # 验证图片绝不进入 SYSTEM，且紧跟在 <untrusted_input_file_metadata> 之后的 USER 消息中。
    assert all(not m.has_image_parts() for m in llm_messages if m.role is LlmRole.SYSTEM)
    meta_idx = next(
        idx for idx, m in enumerate(llm_messages) if "<untrusted_input_file_metadata>" in m.content
    )
    img_msg = llm_messages[meta_idx + 1]
    assert img_msg.role is LlmRole.USER
    assert img_msg.has_image_parts()
    assert isinstance(img_msg.content_parts[0], LlmTextPart)
    assert "不可信数据" in img_msg.content_parts[0].text
    assert any(
        isinstance(p, LlmImagePart) and p.data_base64 == huge_b64 for p in img_msg.content_parts
    )


def test_compression_counts_vision_tokens_for_main_budget_but_sends_zero_images_to_compressor() -> (
    None
):
    """图片占用主请求预算从而触发历史压缩；发往压缩模型的请求零 LlmImagePart 且保留纯文本占位。"""
    main_runtime = _runtime_profile(
        profile_id="mpv_main",
        supports_vision=True,
        context_window_tokens=4096,
        max_output_tokens=256,
    )
    compression_runtime = _runtime_profile(
        profile_id="mpv_cmp",
        supports_vision=False,
        context_window_tokens=4096,
        max_output_tokens=256,
    )
    policy = CompressionPolicyVersion(
        id="cpv_1",
        compression_model_profile_id="mpv_cmp",
        trigger_tokens=1600,
        recent_ratio=0.1,
        recent_token_cap=60,
        summary_ratio=0.15,
        summary_token_cap=200,
        created_at=NOW,
    )
    run = _sample_run(
        model_profile_id="mpv_main",
        compression_policy_version_id="cpv_1",
        trigger_message_id="msg_3",
    )
    lease = RunLease(
        run=run,
        lease_expires_at=NOW + timedelta(seconds=60),
        execution_deadline_at=NOW + timedelta(seconds=60),
    )

    # 历史消息含两轮旧对话（首轮含旧图片附件引用且长度大于 recent_token_cap=60），以及触发消息；
    # 无图片时文本总 Token 低于 trigger_tokens=1600 不触发压缩，
    # 加上 2 张 1024x1024 图片（2 * 765 = 1530 tokens）后会超过 1600 并触发首轮历史压缩。
    old_user = ConversationMessage(
        id="msg_1",
        session_id=run.session_id,
        sequence=1,
        role=ConversationRole.USER,
        status=MessageStatus.COMPLETED,
        parts=(
            TextPart("历史轮次：请先看这张旧报表。" + "详细财务背景说明与同比数据记录。" * 10),
            FileRefPart(
                file_id="file_old_img",
                filename="old_chart.png",
                mime_type="image/png",
                size_bytes=1234,
            ),
        ),
        reply_to_message_id=None,
        run_id="run_prev",
        client_message_id=None,
        created_at=NOW,
        completed_at=NOW,
        metadata=MessageMetadata(),
        context_turn_id="msg_1",
    )
    old_assistant = ConversationMessage(
        id="msg_2",
        session_id=run.session_id,
        sequence=2,
        role=ConversationRole.ASSISTANT,
        status=MessageStatus.COMPLETED,
        parts=(TextPart("历史回复：旧报表营收增长 15%，核心毛利率保持稳定。" * 5),),
        reply_to_message_id="msg_1",
        run_id="run_prev",
        client_message_id=None,
        created_at=NOW,
        completed_at=NOW,
        metadata=MessageMetadata(),
        context_turn_id="msg_1",
    )
    trigger_user = ConversationMessage(
        id="msg_3",
        session_id=run.session_id,
        sequence=3,
        role=ConversationRole.USER,
        status=MessageStatus.COMPLETED,
        parts=(TextPart("现在结合新图继续分析。"),),
        reply_to_message_id=None,
        run_id=run.id,
        client_message_id=None,
        created_at=NOW,
        completed_at=NOW,
        metadata=MessageMetadata(),
        context_turn_id="msg_3",
    )
    history = (old_user, old_assistant, trigger_user)
    dummy_b64 = base64.b64encode(b"img-bytes").decode("ascii")
    images = (
        RunInputImageAttachment("file_new_1", "new1.png", "image/png", dummy_b64, 1024, 1024),
        RunInputImageAttachment("file_new_2", "new2.png", "image/png", dummy_b64, 1024, 1024),
    )

    class _CmpRepo:
        def __init__(self) -> None:
            self.events: list[RunEvent] = []
            self.saved_summaries: list[SaveContextSummaryCommand] = []

        async def list_context_summaries(
            self, session_id: str, owner_id: str, run_id: str
        ) -> tuple[ContextSummary, ...]:
            del session_id, owner_id, run_id
            return ()

        async def list_events(self, *args: Any, **kwargs: Any) -> tuple[RunEvent, ...]:
            del args, kwargs
            return ()

        async def append_compression_event(
            self, run_id: str, worker_id: str, execution_epoch: int, payload: Any
        ) -> RunEvent:
            del worker_id, execution_epoch
            event = RunEvent(
                id=f"ev_{len(self.events) + 1}",
                run_id=run_id,
                sequence=len(self.events) + 2,
                type=payload.type,
                created_at=NOW,
                payload=payload,
            )
            self.events.append(event)
            return event

        async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
            del run_id, execution_epoch
            return True

        async def record_usage(self, command: RecordRunUsageCommand) -> Run:
            del command
            return run

        async def save_context_summary(
            self, command: SaveContextSummaryCommand
        ) -> tuple[ContextSummary, RunEvent]:
            self.saved_summaries.append(command)
            summary = ContextSummary(
                id=command.summary_id,
                session_id=run.session_id,
                owner_id=run.owner_id,
                covered_through_sequence=command.covered_through_sequence,
                parent_summary_id=command.parent_summary_id,
                text=command.text,
                run_id=run.id,
                execution_epoch=run.execution_epoch,
                operation_id=command.operation_id,
                policy_version_id=command.policy_version_id,
                compression_model_version_id=command.compression_model_version_id,
                compression_prompt_version_id=run.compression_prompt_version_id,
                created_at=command.created_at,
            )
            event = RunEvent(
                id="ev_done",
                run_id=run.id,
                sequence=len(self.events) + 2,
                type="compression_completed",
                created_at=NOW,
                payload=SimpleNamespace(type="compression_completed"),  # type: ignore[arg-type]
            )
            return summary, event

    class _CmpProfiles:
        async def get_runtime(self, profile_id: str) -> RuntimeModelProfile | None:
            if profile_id == "mpv_cmp":
                return compression_runtime
            if profile_id == "mpv_main":
                return main_runtime
            return None

    compression_requests: list[LlmRequest] = []

    class _CmpProvider:
        async def complete(self, runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
            del runtime
            compression_requests.append(request)
            return LlmResponse(
                output=(LlmText("摘要：旧报表营收增长 15%。"),),
                status="completed",
                incomplete_reason=None,
                model="cmp-model",
                response_id="resp_cmp",
                request_id=None,
                usage=LlmUsage(input_tokens=100, output_tokens=20, total_tokens=120),
            )

    repo = _CmpRepo()
    compressor = ContextCompressor(
        cast(RunRepository, repo),
        cast(ModelProfileRepository, _CmpProfiles()),
        cast(LlmRuntimeTextProvider, _CmpProvider()),
    )

    # 先验证无图片时不触发压缩（低于 trigger_tokens=1600）。
    prep_no_img = asyncio.run(
        compressor.prepare(
            lease,
            LlmRequestSpec(
                run=run,
                messages=history,
                main_prompt="你是助手",
                tools=(),
                input_images=(),
            ),
            main_runtime,
            policy,
            compression_prompt="请压缩历史",
            timeout_seconds=15.0,
        )
    )
    assert not prep_no_img.compression_called
    assert compression_requests == []

    # 带 2 张图片时，主预算计入 1530 视觉 Token -> 超过 1600 -> 触发历史压缩。
    prep_with_img = asyncio.run(
        compressor.prepare(
            lease,
            LlmRequestSpec(
                run=run,
                messages=history,
                main_prompt="你是助手",
                tools=(),
                input_images=images,
            ),
            main_runtime,
            policy,
            compression_prompt="请压缩历史",
            timeout_seconds=15.0,
        )
    )
    assert prep_with_img.compression_completed
    assert len(compression_requests) == 1
    sent_to_compressor = compression_requests[0]
    # 压缩隔离铁律：发往压缩模型的请求零 LlmImagePart，历史附件保留纯文本占位。
    assert not sent_to_compressor.has_image_parts()
    assert "[附件：old_chart.png]" in sent_to_compressor.messages[1].content
    assert dummy_b64 not in sent_to_compressor.messages[1].content
    # 压缩后的主模型消息仍保留当前 Run 的 2 张视觉图片。
    image_parts = [
        part
        for msg in prep_with_img.messages
        for part in msg.content_parts
        if isinstance(part, LlmImagePart)
    ]
    assert len(image_parts) == 2


# ---------------------------------------------------------------------------
# 四、RunExecutor 端到端测试（缓存、5 张上限、单图容错、A2 降级与 SafeStop 隔离）
# ---------------------------------------------------------------------------


@dataclass
class _FakePromptRepo:
    async def get_pair_by_ids(
        self, main_id: str, cmp_id: str
    ) -> tuple[SystemPromptVersion, SystemPromptVersion]:
        return (
            SystemPromptVersion(
                id=main_id,
                kind=SystemPromptKind.MAIN,
                content="主系统提示词",
                created_by="usr_m32",
                created_at=NOW,
                restored_from_version_id=None,
            ),
            SystemPromptVersion(
                id=cmp_id,
                kind=SystemPromptKind.COMPRESSION,
                content="压缩系统提示词",
                created_by="usr_m32",
                created_at=NOW,
                restored_from_version_id=None,
            ),
        )


class _E2ERunRepository:
    """支持多轮工具调用、安全停止与最终回复持久化的轻量内存 RunRepository。"""

    def __init__(self, run: Run, trigger_message: ConversationMessage) -> None:
        self.run = run
        self.messages: list[ConversationMessage | ExecutionMessage] = [trigger_message]
        self.records: dict[str, RunToolCallRecord] = {}
        self.final_command: FinishRunCommand | None = None
        self.fail_command: FailRunCommand | None = None
        self.recorded_usages: list[RecordRunUsageCommand] = []
        self._next_seq = trigger_message.sequence + 1

    async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
        return run_id == self.run.id and execution_epoch == self.run.execution_epoch

    async def initialize_tools(self, command: InitializeRunToolsCommand) -> RuntimeToolSnapshot:
        self.run = replace(self.run, tool_snapshot=command.snapshot)
        return command.snapshot

    async def get_resumable_tool_batch(self, run_id: str) -> RunToolBatch | None:
        del run_id
        return None

    async def list_context_messages(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[ConversationMessage | ExecutionMessage, ...]:
        del session_id, owner_id, run_id
        return tuple(self.messages)

    async def list_successfully_loaded_skill_ids(self, run_id: str) -> tuple[str, ...]:
        del run_id
        return ()

    async def record_usage(self, command: RecordRunUsageCommand) -> Run:
        self.recorded_usages.append(command)
        if command.llm_request_count is not None:
            self.run = replace(
                self.run,
                llm_request_count=max(self.run.llm_request_count, command.llm_request_count),
            )
        return self.run

    async def begin_tool_call_batch(
        self, command: BeginToolCallBatchCommand
    ) -> BeginToolCallBatchResult:
        self.messages.append(
            ExecutionMessage(
                id=command.call_message.id,
                session_id=self.run.session_id,
                run_id=self.run.id,
                thread_id=command.call_message.thread_id,
                sequence=self._next_seq,
                role=ExecutionRole.ASSISTANT,
                parts=command.call_message.parts,
                tool_call_id=command.call_message.tool_call_id,
                source_message_id=command.call_message.source_message_id,
                created_at=command.requested_at,
                metadata=command.call_message.metadata,
            )
        )
        self._next_seq += 1
        call_parts = [part for part in command.call_message.parts if isinstance(part, ToolCallPart)]
        batch_calls: list[RunToolBatchCall] = []
        for ordinal, (item, part) in enumerate(
            zip(command.calls, call_parts, strict=True), start=1
        ):
            record = RunToolCallRecord(
                id=item.record_id,
                run_id=command.run_id,
                session_id=command.call_message.session_id,
                tool_call_id=item.tool_call_id,
                ordinal=ordinal,
                tool_id=item.tool_id,
                tool_kind=item.tool_kind,
                model_name=item.model_name,
                execution_epoch=command.execution_epoch,
                status=ToolCallStatus.APPROVED,
                call_message_id=command.call_message.id,
                result_message_id=None,
                started_at=None,
                finished_at=None,
                safe_error_code=None,
                outcome=None,
                requires_approval=item.requires_approval,
                requested_at=command.requested_at,
            )
            self.records[record.id] = record
            batch_calls.append(RunToolBatchCall(record=record, arguments=dict(part.arguments)))
        batch = RunToolBatch(
            call_message_id=command.call_message.id,
            thread_id=command.call_message.thread_id,
            round=command.round,
            calls=tuple(batch_calls),
        )
        return BeginToolCallBatchResult(batch, False)

    async def admit_tool_call(self, command: Any) -> RunToolCallRecord:
        record = replace(
            self.records[command.record_id],
            status=ToolCallStatus.EXECUTING,
            started_at=command.admitted_at,
        )
        self.records[record.id] = record
        return record

    async def finish_tool_call(self, command: FinishToolCallCommand) -> Run:
        record = replace(
            self.records[command.record_id],
            status=ToolCallStatus.COMPLETED,
            result_message_id=command.result_message.id,
            finished_at=command.finished_at,
            outcome=command.outcome,
        )
        self.records[record.id] = record
        self.messages.append(
            ExecutionMessage(
                id=command.result_message.id,
                session_id=self.run.session_id,
                run_id=self.run.id,
                thread_id=command.result_message.thread_id,
                sequence=self._next_seq,
                role=ExecutionRole.TOOL,
                parts=command.result_message.parts,
                tool_call_id=command.result_message.tool_call_id,
                source_message_id=command.result_message.source_message_id,
                created_at=command.finished_at,
                metadata=command.result_message.metadata,
            )
        )
        self._next_seq += 1
        return self.run

    async def finish(self, command: FinishRunCommand) -> Run:
        self.final_command = command
        self.run = replace(self.run, status=RunStatus.COMPLETED)
        return self.run

    async def fail(self, command: FailRunCommand) -> Run:
        self.fail_command = command
        self.run = replace(
            self.run, status=RunStatus.FAILED, safe_error_code=command.safe_error_code
        )
        return self.run

    async def list_tool_calls(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None

    async def list_events(self, *args: Any, **kwargs: Any) -> tuple[RunEvent, ...]:
        del args, kwargs
        return ()


class _ScriptedStreamProvider:
    """按轮次返回预设流式响应，同时记录每次收到的完整 LlmRequest。"""

    def __init__(self, responses: Sequence[LlmResponse]) -> None:
        self._responses = list(responses)
        self.stream_requests: list[LlmRequest] = []
        self.complete_requests: list[LlmRequest] = []

    @asynccontextmanager
    async def stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncIterator[AsyncIterator[LlmStreamEvent]]:
        del runtime
        self.stream_requests.append(request)
        response = self._responses.pop(0)

        async def _iter() -> AsyncIterator[LlmStreamEvent]:
            for item in response.output:
                if isinstance(item, LlmText):
                    yield TextDelta(output_index=0, content_index=0, text=item.text)
            yield ResponseCompleted(response)

        yield _iter()

    async def complete(self, runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
        del runtime
        self.complete_requests.append(request)
        return LlmResponse(
            output=(LlmText("ignored"),),
            status="completed",
            incomplete_reason=None,
            model="test-model",
            response_id="resp_cmp",
            request_id=None,
            usage=LlmUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )


def _build_real_tool_runtime(
    session_files: SessionFileService,
) -> RunToolRuntimeService:
    """使用真实 RunToolRuntimeService + 轻量认证/会话替身执行完整视觉上下文构建。"""
    auth = SimpleNamespace(
        get_current_session_user=lambda owner_id, auth_sid, now: asyncio.sleep(
            0, result=SimpleNamespace(id=owner_id)
        )
    )
    sessions = SimpleNamespace(
        get_visible=lambda scope: asyncio.sleep(
            0, result=SimpleNamespace(id=scope.session_id, project_id=None)
        )
    )
    mcp_catalog = SimpleNamespace(
        list_readable_runtime_tools=lambda snapshot, limit=201: asyncio.sleep(0, result=())
    )
    mcp_executor = SimpleNamespace()
    return RunToolRuntimeService(
        auth=cast(Any, auth),
        sessions=cast(Any, sessions),
        mcp_catalog=cast(Any, mcp_catalog),
        mcp_executor=cast(Any, mcp_executor),
        knowledge=None,
        project_knowledge=None,
        session_files=session_files,
        access_policy=make_test_access_policy(),
    )


def _extract_input_file_metadata(request: LlmRequest) -> list[dict[str, Any]]:
    user_msg = next(
        m.content for m in request.messages if "<untrusted_input_file_metadata>" in m.content
    )
    segment = user_msg.split("<untrusted_input_file_metadata>\n", 1)[1].split(
        "\n</untrusted_input_file_metadata>", 1
    )[0]
    return cast(list[dict[str, Any]], json.loads(segment))


def test_run_executor_vision_enabled_caches_caps_five_and_isolates_failures() -> None:
    """supports_vision=True：多轮工具循环仅加载一次、上限 5 张、损坏/超大图片不阻断其余有效图片。"""
    png_bytes = _encode_png_with_privacy_metadata(320, 240, orientation=1, alpha=False)
    jpg_bytes = _encode_jpeg_with_privacy_metadata(320, 240, orientation=1)

    files: list[SessionFile] = [
        _session_file("file_corrupt", "broken.png", "image/png", b"not-a-png"),
        _session_file(
            "file_oversized",
            "huge.jpg",
            "image/jpeg",
            jpg_bytes,
            size_bytes_override=MAX_VISION_RAW_IMAGE_BYTES + 1024,
        ),
        _session_file("file_img_1", "img1.png", "image/png", png_bytes),
        _session_file("file_img_2", "img2.jpg", "image/jpeg", jpg_bytes),
        _session_file("file_img_3", "img3.png", "image/png", png_bytes),
        _session_file("file_img_4", "img4.jpg", "image/jpeg", jpg_bytes),
        _session_file("file_img_5", "img5.png", "image/png", png_bytes),
        _session_file("file_img_6", "img6.png", "image/png", png_bytes),
        _session_file("file_note", "notes.txt", "text/plain", b"hello notes"),
    ]
    blobs = {
        files[0].object_key: b"not-a-png",
        files[1].object_key: jpg_bytes,
        files[2].object_key: png_bytes,
        files[3].object_key: jpg_bytes,
        files[4].object_key: png_bytes,
        files[5].object_key: jpg_bytes,
        files[6].object_key: png_bytes,
        files[7].object_key: png_bytes,
        files[8].object_key: b"hello notes",
    }
    store = _InMemoryObjectStore(blobs)
    file_service = SessionFileService(
        cast(SessionFileRepository, _InMemorySessionFileRepo(files)),
        cast(FileObjectStore, store),
        image_normalizer=LocalVisionImageNormalizer(),
    )
    tool_runtime = _build_real_tool_runtime(file_service)

    # 给快照装配一个无审批的内置测试工具，模拟两轮 LLM 调用循环。
    dummy_tool_def = RuntimeToolDefinition(
        tool_id="builtin_dummy_echo",
        model_name="dummy_echo",
        description="测试回声工具",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        source=BuiltinToolSource(version=1),
        requires_approval=False,
    )

    async def _fake_execute(*_args: Any, **_kwargs: Any) -> ToolExecutionResult:
        return ToolExecutionResult(
            ToolOutcome(status=ToolOutcomeStatus.OK, code="ok", message="done", data={"ok": True})
        )

    tool_runtime.execute = _fake_execute  # type: ignore[method-assign]

    snapshot = RuntimeToolSnapshot(tools=(REQUEST_USER_INPUT_DEFINITION, dummy_tool_def))
    run = _sample_run(
        input_file_ids=tuple(item.id for item in files),
        tool_snapshot=snapshot,
    )
    trigger = ConversationMessage(
        id=run.trigger_message_id,
        session_id=run.session_id,
        sequence=1,
        role=ConversationRole.USER,
        status=MessageStatus.COMPLETED,
        parts=(TextPart("请看这些图片并结合工具回答。"),),
        reply_to_message_id=None,
        run_id=run.id,
        client_message_id=None,
        created_at=NOW,
        completed_at=NOW,
        metadata=MessageMetadata(),
        context_turn_id=run.trigger_message_id,
    )
    repo = _E2ERunRepository(run, trigger)
    runtime_profile = _runtime_profile(supports_vision=True)
    profiles = SimpleNamespace(
        get_runtime=lambda profile_id: asyncio.sleep(0, result=runtime_profile),
        get_compression_policy=lambda policy_id: asyncio.sleep(0, result=None),
    )
    provider = _ScriptedStreamProvider(
        [
            LlmResponse(
                output=(LlmToolCall("call_1", "dummy_echo", {}),),
                status="completed",
                incomplete_reason=None,
                model="gpt-4o-test",
                response_id="resp_1",
                request_id=None,
                usage=LlmUsage(input_tokens=1500, output_tokens=20, total_tokens=1520),
            ),
            LlmResponse(
                output=(LlmText("已查看 5 张有效图片并完成汇总。"),),
                status="completed",
                incomplete_reason=None,
                model="gpt-4o-test",
                response_id="resp_2",
                request_id=None,
                usage=LlmUsage(input_tokens=1600, output_tokens=30, total_tokens=1630),
            ),
        ]
    )

    executor = RunExecutor(
        cast(RunRepository, repo),
        cast(ModelProfileRepository, profiles),
        cast(LlmStreamProvider, provider),
        system_prompts=cast(Any, _FakePromptRepo()),
        tool_runtime=cast(RunToolRuntime, tool_runtime),
        config=RunWorkerConfig(),
        clock=lambda: NOW,
    )
    lease = RunLease(
        run=run,
        lease_expires_at=NOW + timedelta(seconds=60),
        execution_deadline_at=NOW + timedelta(seconds=60),
    )

    asyncio.run(executor.execute(lease))

    assert repo.final_command is not None
    assert repo.fail_command is None
    assert len(provider.stream_requests) == 2

    # 校验缓存：跨两轮模型调用，仅读取过 1 次损坏图 + 5 次有效图 = 共 6 次对象存储读取；
    # 超限元数据大图与第 6 张有效图（img6）均未触发对象存储读取！
    downloaded_keys = [key for key, _ in store.get_bytes_calls]
    assert len(downloaded_keys) == 6
    assert files[1].object_key not in downloaded_keys  # file_oversized
    assert files[7].object_key not in downloaded_keys  # file_img_6 (skipped_limit)
    assert files[8].object_key not in downloaded_keys  # file_note (text)

    # 校验两轮发给模型的请求均携带 5 张多模态图片，且元数据状态精准。
    for req in provider.stream_requests:
        meta = _extract_input_file_metadata(req)
        by_id = {item["file_id"]: item for item in meta}
        assert by_id["file_corrupt"]["vision_status"] == "image_invalid"
        assert by_id["file_oversized"]["vision_status"] == "image_too_large"
        for idx in range(1, MAX_VISION_IMAGES_PER_RUN + 1):
            assert by_id[f"file_img_{idx}"]["vision_status"] == "attached"
        assert by_id["file_img_6"]["vision_status"] == "skipped_limit"
        assert "vision_status" not in by_id["file_note"]

        image_parts = [
            part
            for msg in req.messages
            for part in msg.content_parts
            if isinstance(part, LlmImagePart)
        ]
        assert len(image_parts) == MAX_VISION_IMAGES_PER_RUN
        assert all(not msg.has_image_parts() for msg in req.messages if msg.role is LlmRole.SYSTEM)


def test_run_executor_non_vision_model_gracefully_degrades_without_reading_object_store() -> None:
    """方案 A2：supports_vision=False 温柔降级、不读图片字节、注入元数据状态与系统提示引导。"""
    png_bytes = _encode_png_with_privacy_metadata(128, 128, orientation=1, alpha=False)
    f_img = _session_file("file_img_a2", "diagram.png", "image/png", png_bytes)
    f_txt = _session_file("file_txt_a2", "readme.txt", "text/plain", b"plain text")
    store = _InMemoryObjectStore({f_img.object_key: png_bytes, f_txt.object_key: b"plain text"})
    file_service = SessionFileService(
        cast(SessionFileRepository, _InMemorySessionFileRepo((f_img, f_txt))),
        cast(FileObjectStore, store),
        image_normalizer=LocalVisionImageNormalizer(),
    )
    tool_runtime = _build_real_tool_runtime(file_service)

    run = _sample_run(
        model_profile_id="mpv_text_only",
        input_file_ids=(f_img.id, f_txt.id),
        tool_snapshot=RuntimeToolSnapshot(tools=(REQUEST_USER_INPUT_DEFINITION,)),
    )
    trigger = ConversationMessage(
        id=run.trigger_message_id,
        session_id=run.session_id,
        sequence=1,
        role=ConversationRole.USER,
        status=MessageStatus.COMPLETED,
        parts=(TextPart("图里写了什么？"),),
        reply_to_message_id=None,
        run_id=run.id,
        client_message_id=None,
        created_at=NOW,
        completed_at=NOW,
        metadata=MessageMetadata(),
        context_turn_id=run.trigger_message_id,
    )
    repo = _E2ERunRepository(run, trigger)
    non_vision_profile = _runtime_profile(profile_id="mpv_text_only", supports_vision=False)
    profiles = SimpleNamespace(
        get_runtime=lambda profile_id: asyncio.sleep(0, result=non_vision_profile),
        get_compression_policy=lambda policy_id: asyncio.sleep(0, result=None),
    )
    provider = _ScriptedStreamProvider(
        [
            LlmResponse(
                output=(LlmText("当前模型暂不支持识图，建议切换至支持识图的模型后重试。"),),
                status="completed",
                incomplete_reason=None,
                model="text-only-model",
                response_id="resp_a2",
                request_id=None,
                usage=LlmUsage(input_tokens=200, output_tokens=25, total_tokens=225),
            )
        ]
    )

    executor = RunExecutor(
        cast(RunRepository, repo),
        cast(ModelProfileRepository, profiles),
        cast(LlmStreamProvider, provider),
        system_prompts=cast(Any, _FakePromptRepo()),
        tool_runtime=cast(RunToolRuntime, tool_runtime),
        config=RunWorkerConfig(),
        clock=lambda: NOW,
    )
    lease = RunLease(
        run=run,
        lease_expires_at=NOW + timedelta(seconds=60),
        execution_deadline_at=NOW + timedelta(seconds=60),
    )

    asyncio.run(executor.execute(lease))

    assert repo.final_command is not None
    assert repo.fail_command is None
    # 绝不从对象存储读取任何图片字节。
    assert store.get_bytes_calls == []
    assert len(provider.stream_requests) == 1
    req = provider.stream_requests[0]
    assert not req.has_image_parts()

    meta = _extract_input_file_metadata(req)
    by_id = {item["file_id"]: item for item in meta}
    assert by_id["file_img_a2"]["vision_status"] == "unsupported_by_current_model"
    assert "vision_status" not in by_id["file_txt_a2"]

    system_file_msg = next(
        m.content
        for m in req.messages
        if m.role is LlmRole.SYSTEM and "session_file_read" in m.content
    )
    assert "当前所选模型不支持直接查看图片内容" in system_file_msg
    assert "切换至支持识图的模型后重试" in system_file_msg


def test_safe_stop_report_records_vision_status_and_never_persists_base64() -> None:
    """触发安全停止时，persisted_state 与落库报告保留安全 vision_status 且零 base64 泄露。"""
    secret_b64 = base64.b64encode(b"SUPER-SECRET-IMAGE-DATA-NEVER-PERSIST").decode("ascii")
    png_bytes = _encode_png_with_privacy_metadata(1024, 1024, orientation=1, alpha=False)
    f_img = _session_file("file_stop_img", "chart.png", "image/png", png_bytes)

    # 使用极小上下文窗口迫使带图请求直接触发安全停止（HARD_WINDOW_EXCEEDED）。
    tiny_vision_profile = _runtime_profile(
        profile_id="mpv_tiny_vision",
        supports_vision=True,
        context_window_tokens=600,
        max_output_tokens=100,
    )

    class _StaticNormalizer:
        async def normalize(
            self,
            *,
            file_id: str,
            filename: str,
            declared_mime_type: str,
            raw_bytes: bytes,
        ) -> NormalizedVisionImage:
            del declared_mime_type, raw_bytes
            return NormalizedVisionImage(
                file_id=file_id,
                filename=filename,
                mime_type="image/png",
                data_base64=secret_b64,
                width=1024,
                height=1024,
                size_bytes=64,
            )

    store = _InMemoryObjectStore({f_img.object_key: png_bytes})
    file_service = SessionFileService(
        cast(SessionFileRepository, _InMemorySessionFileRepo((f_img,))),
        cast(FileObjectStore, store),
        image_normalizer=_StaticNormalizer(),
    )
    tool_runtime = _build_real_tool_runtime(file_service)
    run = _sample_run(
        model_profile_id="mpv_tiny_vision",
        input_file_ids=(f_img.id,),
        tool_snapshot=RuntimeToolSnapshot(tools=(REQUEST_USER_INPUT_DEFINITION,)),
    )
    trigger = ConversationMessage(
        id=run.trigger_message_id,
        session_id=run.session_id,
        sequence=1,
        role=ConversationRole.USER,
        status=MessageStatus.COMPLETED,
        parts=(TextPart("分析这张图"),),
        reply_to_message_id=None,
        run_id=run.id,
        client_message_id=None,
        created_at=NOW,
        completed_at=NOW,
        metadata=MessageMetadata(),
        context_turn_id=run.trigger_message_id,
    )
    repo = _E2ERunRepository(run, trigger)
    profiles = SimpleNamespace(
        get_runtime=lambda profile_id: asyncio.sleep(0, result=tiny_vision_profile),
        get_compression_policy=lambda policy_id: asyncio.sleep(0, result=None),
    )
    provider = _ScriptedStreamProvider([])
    executor = RunExecutor(
        cast(RunRepository, repo),
        cast(ModelProfileRepository, profiles),
        cast(LlmStreamProvider, provider),
        system_prompts=cast(Any, _FakePromptRepo()),
        tool_runtime=cast(RunToolRuntime, tool_runtime),
        config=RunWorkerConfig(),
        clock=lambda: NOW,
    )
    lease = RunLease(
        run=run,
        lease_expires_at=NOW + timedelta(seconds=60),
        execution_deadline_at=NOW + timedelta(seconds=60),
    )

    asyncio.run(executor.execute(lease))

    assert repo.fail_command is not None
    assert repo.fail_command.safe_error_code == "run_context_safety_stop"
    assert repo.fail_command.context_stop_reason is ContextStopReason.HARD_WINDOW_EXCEEDED
    assert repo.fail_command.report_message is not None
    assert repo.fail_command.report_message.metadata.provider == "system_context_safety"
    assert repo.fail_command.report_message.metadata.model is None
    report_text = "".join(
        p.text for p in repo.fail_command.report_message.parts if isinstance(p, TextPart)
    )
    assert "上下文超出安全上限，任务未标记为完成" in report_text
    assert secret_b64 not in report_text
    assert all(not req.has_image_parts() for req in provider.complete_requests)


def test_bootstrap_injects_local_vision_image_normalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 API 与 Run Worker 两个组合根均为 SessionFileService 注入 LocalVisionImageNormalizer。"""
    monkeypatch.setenv(
        "ATHENA_DATABASE_URL",
        "postgresql+asyncpg://user:password@127.0.0.1:5432/athena_test",
    )
    monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "minio.example.test:9000")
    monkeypatch.setenv("ATHENA_STORAGE_ACCESS_KEY", "test-ak")
    monkeypatch.setenv("ATHENA_STORAGE_SECRET_KEY", "test-sk")
    monkeypatch.setenv("ATHENA_STORAGE_BUCKET", "athena-test")
    monkeypatch.setenv("ATHENA_STORAGE_SECURE", "false")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv(
        "ATHENA_MCP_SECRET_KEY",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    )
    monkeypatch.setenv("ATHENA_ENV", "development")

    api_deps = build_api_dependencies()
    try:
        assert api_deps.session_file_service is not None
        assert isinstance(
            api_deps.session_file_service._image_normalizer,
            LocalVisionImageNormalizer,
        )
    finally:
        asyncio.run(api_deps.aclose())

    bundle = RunWorkerBundle()
    try:
        build_run_worker_dependencies(bundle)
        assert bundle.worker is not None
        tool_runtime = bundle.worker._executor._tool_runtime
        assert isinstance(tool_runtime, RunToolRuntimeService)
        assert tool_runtime._session_files is not None
        assert isinstance(
            tool_runtime._session_files._image_normalizer,
            LocalVisionImageNormalizer,
        )
    finally:

        async def _cleanup() -> None:
            if bundle.mcp_executor is not None:
                await bundle.mcp_executor.aclose()
            if bundle.skill_package_cache is not None:
                await bundle.skill_package_cache.aclose()
            if bundle.file_object_store is not None:
                await bundle.file_object_store.aclose()
            if bundle.provider is not None:
                await bundle.provider.aclose()
            if bundle.query_services is not None:
                await bundle.query_services.aclose()
            if bundle.storage_client is not None:
                await bundle.storage_client.aclose()
            if bundle.database is not None:
                await bundle.database.dispose()

        asyncio.run(_cleanup())


def test_vision_constants_match_m32_contract() -> None:
    """锁死 M3.2 领域常量契约，防止意外放宽安全边界。"""
    assert VISION_IMAGE_MIME_TYPES == frozenset({"image/png", "image/jpeg", "image/webp"})
    assert MAX_VISION_IMAGES_PER_RUN == 5
    assert MAX_VISION_RAW_IMAGE_BYTES == 20 * 1024 * 1024
    assert MAX_VISION_ENCODED_IMAGE_BYTES == 4 * 1024 * 1024
    assert MAX_VISION_IMAGE_EDGE == 2048
    assert MAX_VISION_IMAGE_PIXELS == 40_000_000
    assert messages_to_llm is not None
