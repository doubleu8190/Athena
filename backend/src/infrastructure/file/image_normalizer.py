"""会话图片安全归一化、方向校正与 EXIF/GPS/ICC/XMP 隐私剥离实现。"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from domain.file import (
    MAX_VISION_ENCODED_IMAGE_BYTES,
    MAX_VISION_IMAGE_EDGE,
    MAX_VISION_IMAGE_PIXELS,
    MAX_VISION_RAW_IMAGE_BYTES,
    VISION_IMAGE_MIME_TYPES,
    NormalizedVisionImage,
    VisionImageError,
    VisionImageMimeType,
    VisionImageNormalizer,
)

logger = logging.getLogger(__name__)

# 单张图片解码、缩放与重编码的默认超时上限（秒），避免异常图片长期占满工作线程。
DEFAULT_VISION_IMAGE_NORMALIZE_TIMEOUT_SECONDS = 15.0
# 超过 4 MiB 体积上限时的质量阶梯与温和缩边倍率（优先保清晰度，拒绝缩成不可读缩略图）。
_QUALITY_LADDER: tuple[int, ...] = (85, 75, 65)
_DOWNSCALE_FACTORS: tuple[float, ...] = (1.0, 0.75, 0.5)
_SUPPORTED_PIL_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})


class LocalVisionImageNormalizer(VisionImageNormalizer):
    """基于 Pillow 的有界图片安全归一化器：防解压炸弹、校正方向、强剥隐私元数据并收敛体积。"""

    def __init__(
        self,
        *,
        max_raw_bytes: int = MAX_VISION_RAW_IMAGE_BYTES,
        max_encoded_bytes: int = MAX_VISION_ENCODED_IMAGE_BYTES,
        max_edge: int = MAX_VISION_IMAGE_EDGE,
        max_pixels: int = MAX_VISION_IMAGE_PIXELS,
        timeout_seconds: float = DEFAULT_VISION_IMAGE_NORMALIZE_TIMEOUT_SECONDS,
    ) -> None:
        """初始化视觉图片安全边界，非法配置在装配期即刻拒绝。"""
        if max_raw_bytes < 1:
            raise ValueError("vision_image_max_raw_bytes_invalid")
        if max_encoded_bytes < 1:
            raise ValueError("vision_image_max_encoded_bytes_invalid")
        if max_edge < 1:
            raise ValueError("vision_image_max_edge_invalid")
        if max_pixels < 1:
            raise ValueError("vision_image_max_pixels_invalid")
        if timeout_seconds <= 0:
            raise ValueError("vision_image_timeout_invalid")
        self._max_raw_bytes = max_raw_bytes
        self._max_encoded_bytes = max_encoded_bytes
        self._max_edge = max_edge
        self._max_pixels = max_pixels
        self._timeout_seconds = timeout_seconds

    async def normalize(
        self,
        *,
        file_id: str,
        filename: str,
        declared_mime_type: str,
        raw_bytes: bytes,
    ) -> NormalizedVisionImage:
        """在独立工作线程与超时边界内完成图片校验、隐私剥离与体积收敛，绝不阻塞事件循环。"""
        if declared_mime_type not in VISION_IMAGE_MIME_TYPES:
            raise VisionImageError("vision_image_unsupported")
        if not raw_bytes:
            raise VisionImageError("vision_image_invalid")
        # 打开图片前先按 20 MiB 原始字节上限拦截，避免超大输入进入解码流程。
        if len(raw_bytes) > self._max_raw_bytes:
            raise VisionImageError("vision_image_too_large")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await asyncio.to_thread(
                    self._normalize_sync,
                    file_id,
                    filename,
                    raw_bytes,
                )
        except TimeoutError:
            logger.warning(
                "vision_image_normalize_timeout file_id=%s timeout_seconds=%s",
                file_id,
                self._timeout_seconds,
            )
            raise VisionImageError("vision_image_invalid") from None
        except asyncio.CancelledError:
            raise
        except VisionImageError:
            raise
        except Exception as error:
            logger.warning(
                "vision_image_normalize_failed file_id=%s exception_type=%s",
                file_id,
                type(error).__name__,
            )
            raise VisionImageError("vision_image_invalid") from None

    def _normalize_sync(
        self,
        file_id: str,
        filename: str,
        raw_bytes: bytes,
    ) -> NormalizedVisionImage:
        """在阻塞线程中执行解码防炸弹、EXIF 方向校正、长边缩放、像素拷贝剥离与重编码。"""
        clean_image: Image.Image | None = None
        source_format: str
        try:
            with Image.open(io.BytesIO(raw_bytes)) as opened:
                detected_format = (opened.format or "").upper()
                if detected_format not in _SUPPORTED_PIL_FORMATS:
                    raise VisionImageError("vision_image_unsupported")
                source_format = detected_format
                width, height = opened.size
                if width <= 0 or height <= 0:
                    raise VisionImageError("vision_image_invalid")
                # 在真正解压像素前先校验总像素数，防止小型高压缩比解压炸弹耗尽内存。
                if width * height > self._max_pixels:
                    raise VisionImageError("vision_image_too_large")
                # 先应用 EXIF 方向旋转标记，再剥离 EXIF，保证手机拍摄或截图保持正确朝向。
                transposed = ImageOps.exif_transpose(opened)
                working = opened.copy() if transposed is None else transposed
                try:
                    if working.width <= 0 or working.height <= 0:
                        raise VisionImageError("vision_image_invalid")
                    if working.width * working.height > self._max_pixels:
                        raise VisionImageError("vision_image_too_large")
                    # 按是否包含透明通道归一化像素模式；同时消解调色板与 CMYK 色彩空间依赖。
                    target_mode = "RGBA" if _has_alpha(working) else "RGB"
                    converted = (
                        working if working.mode == target_mode else working.convert(target_mode)
                    )
                    try:
                        resized = _resize_to_max_edge(converted, self._max_edge)
                        try:
                            # 强制剥离全部 EXIF / GPS / ICC / XMP / PNG 文本块等隐私元数据：
                            # 仅复制纯净像素字节构造全新 Image（info 字典为空、无 exif 块）。
                            clean_image = Image.frombytes(
                                resized.mode,
                                resized.size,
                                resized.tobytes(),
                            )
                        finally:
                            if resized is not converted:
                                resized.close()
                    finally:
                        if converted is not working:
                            converted.close()
                finally:
                    working.close()
        except VisionImageError:
            raise
        except Image.DecompressionBombError:
            raise VisionImageError("vision_image_too_large") from None
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError):
            raise VisionImageError("vision_image_invalid") from None

        try:
            mime_type, encoded_bytes, out_width, out_height = self._encode_within_budget(
                clean_image,
                source_format=source_format,
            )
        finally:
            clean_image.close()

        return NormalizedVisionImage(
            file_id=file_id,
            filename=filename,
            mime_type=mime_type,
            data_base64=base64.b64encode(encoded_bytes).decode("ascii"),
            width=out_width,
            height=out_height,
            size_bytes=len(encoded_bytes),
        )

    def _encode_within_budget(
        self,
        clean_image: Image.Image,
        *,
        source_format: str,
    ) -> tuple[VisionImageMimeType, bytes, int, int]:
        """按格式保留透明通道并在 4 MiB 体积上限内收敛输出；无法收敛时抛出安全错误码。"""
        if source_format == "PNG":
            # PNG 优先无损编码以保留透明通道与界面文字锐度。
            png_bytes = _save_image_bytes(clean_image, image_format="PNG", quality=None)
            if len(png_bytes) <= self._max_encoded_bytes:
                return "image/png", png_bytes, clean_image.width, clean_image.height
            # PNG 超 4 MiB 时安全降级：转白底 RGB，按 WEBP/JPEG + 85->75->65 阶梯与适度缩边收敛。
            rgb_image = _flatten_to_white_rgb(clean_image)
            try:
                encoded = _try_lossy_ladder(
                    rgb_image,
                    formats=(("WEBP", "image/webp"), ("JPEG", "image/jpeg")),
                    max_encoded_bytes=self._max_encoded_bytes,
                )
                if encoded is not None:
                    return encoded
            finally:
                if rgb_image is not clean_image:
                    rgb_image.close()
            raise VisionImageError("vision_image_too_large")

        if source_format == "WEBP":
            # WEBP 优先保留原模式（含透明通道）按 85 -> 75 -> 65 质量阶梯尝试收敛。
            direct = _try_quality_ladder(
                clean_image,
                image_format="WEBP",
                mime_type="image/webp",
                max_encoded_bytes=self._max_encoded_bytes,
            )
            if direct is not None:
                return direct
            # 仍超限时先去透明通道转白底 RGB，并结合适度缩边与质量阶梯收敛。
            rgb_image = _flatten_to_white_rgb(clean_image)
            try:
                scales = (
                    _DOWNSCALE_FACTORS if rgb_image is not clean_image else _DOWNSCALE_FACTORS[1:]
                )
                encoded = _try_lossy_ladder(
                    rgb_image,
                    formats=(("WEBP", "image/webp"),),
                    max_encoded_bytes=self._max_encoded_bytes,
                    downscale_factors=scales,
                )
                if encoded is not None:
                    return encoded
            finally:
                if rgb_image is not clean_image:
                    rgb_image.close()
            raise VisionImageError("vision_image_too_large")

        # JPEG 固定输出白底 RGB，按 85 -> 75 -> 65 质量阶梯与适度缩边尝试收敛至 4 MiB 内。
        rgb_image = _flatten_to_white_rgb(clean_image)
        try:
            encoded = _try_lossy_ladder(
                rgb_image,
                formats=(("JPEG", "image/jpeg"),),
                max_encoded_bytes=self._max_encoded_bytes,
            )
            if encoded is not None:
                return encoded
        finally:
            if rgb_image is not clean_image:
                rgb_image.close()
        raise VisionImageError("vision_image_too_large")


def _has_alpha(image: Image.Image) -> bool:
    """判断图片是否携带透明通道或调色板/键色透明信息。"""
    return image.mode in {"RGBA", "LA", "PA", "RGBa", "La"} or "transparency" in image.info


def _resize_to_max_edge(image: Image.Image, max_edge: int) -> Image.Image:
    """长边超过上限时按原宽高比使用 LANCZOS 缩放，不放大低分辨率图片。"""
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        return image
    scale = max_edge / longest
    target_width = min(max_edge, max(1, round(width * scale)))
    target_height = min(max_edge, max(1, round(height * scale)))
    return image.resize((target_width, target_height), Image.Resampling.LANCZOS)


def _flatten_to_white_rgb(image: Image.Image) -> Image.Image:
    """将带透明通道的图片合成到纯白 RGB 背景上，并确保返回全新无元数据像素副本。"""
    if image.mode == "RGB":
        return image
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    try:
        background.paste(rgba, mask=rgba.getchannel("A"))
        return Image.frombytes("RGB", background.size, background.tobytes())
    finally:
        background.close()
        if rgba is not image:
            rgba.close()


def _save_image_bytes(
    image: Image.Image,
    *,
    image_format: Literal["PNG", "JPEG", "WEBP"],
    quality: int | None,
) -> bytes:
    """以不附带任何 exif / icc_profile / xmp / pnginfo 的纯净参数重编码图片。"""
    buffer = io.BytesIO()
    if image_format == "PNG":
        image.save(buffer, format="PNG", optimize=True)
    elif image_format == "JPEG":
        image.save(buffer, format="JPEG", quality=quality or 85, optimize=True)
    else:
        image.save(buffer, format="WEBP", quality=quality or 85)
    return buffer.getvalue()


def _try_quality_ladder(
    image: Image.Image,
    *,
    image_format: Literal["JPEG", "WEBP"],
    mime_type: VisionImageMimeType,
    max_encoded_bytes: int,
) -> tuple[VisionImageMimeType, bytes, int, int] | None:
    """在当前分辨率下按 85 -> 75 -> 65 质量阶梯尝试收敛至目标字节上限内。"""
    for quality in _QUALITY_LADDER:
        encoded = _save_image_bytes(image, image_format=image_format, quality=quality)
        if len(encoded) <= max_encoded_bytes:
            return mime_type, encoded, image.width, image.height
    return None


def _try_lossy_ladder(
    image: Image.Image,
    *,
    formats: tuple[tuple[Literal["JPEG", "WEBP"], VisionImageMimeType], ...],
    max_encoded_bytes: int,
    downscale_factors: tuple[float, ...] = _DOWNSCALE_FACTORS,
) -> tuple[VisionImageMimeType, bytes, int, int] | None:
    """按温和缩边倍率与 85 -> 75 -> 65 质量阶梯依次尝试各候选有损格式收敛体积。"""
    for factor in downscale_factors:
        candidate = image
        if factor < 1.0:
            target_width = max(1, round(image.width * factor))
            target_height = max(1, round(image.height * factor))
            if target_width == image.width and target_height == image.height:
                continue
            candidate = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
        try:
            for image_format, mime_type in formats:
                matched = _try_quality_ladder(
                    candidate,
                    image_format=image_format,
                    mime_type=mime_type,
                    max_encoded_bytes=max_encoded_bytes,
                )
                if matched is not None:
                    return matched
        finally:
            if candidate is not image:
                candidate.close()
    return None


__all__ = [
    "DEFAULT_VISION_IMAGE_NORMALIZE_TIMEOUT_SECONDS",
    "LocalVisionImageNormalizer",
]
