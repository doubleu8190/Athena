#!/usr/bin/env python3
"""星辰智联企业测试数据体系（V1 + V2 + V3）确定性多格式资产生成器。

职责：
1. 从 ``sources/`` 与 ``skills/``（SK-5、SK-7）确定性生成全部 6 库 36 档 KB 多格式样本
   （含 DOC-01-V2、DOC-13-V2 两份版本替换样本）、19 份会话多模态及边界附件
   （多页扫描件 PDF、WebP、加密 PDF、含宏 DOCX、损坏 PNG）及两套 Skill ZIP 至 ``generated/``。
2. 纯原生中文可选中 PDF 采用 Type0 CIDFontType2 + Identity-H + 分块 beginbfchar ToUnicode CMap，
   不嵌入整页位图，确保 ``LocalDocumentParser`` 毫秒级精准抽取中文且不误触发 PaddleOCR。
3. 无文本层单页/多页扫描件 PDF 仅嵌入光栅 JPEG XObject，用于验证无 OCR 路径空文本降级。
4. 统一归一化 Office/Skill ZIP 成员时间戳并输出 ``generated/checksums.json``，
   保证多次生成字节完全一致且总体积控制在 4 MiB 预算内。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import textwrap
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from docx import Document as DocxDocument
from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]
from PIL import Image, ImageDraw, ImageFont
from PIL.ExifTags import GPS, Base
from pptx import Presentation
from pptx.util import Inches, Pt

DATASET_ROOT = Path(__file__).resolve().parents[1]
SOURCES_DIR = DATASET_ROOT / "sources"
KB_SOURCES_DIR = SOURCES_DIR / "kb"
SKILL_SOURCE_DIR = DATASET_ROOT / "skills" / "hroa_lifecycle_audit"
TRAVEL_SKILL_SOURCE_DIR = DATASET_ROOT / "skills" / "travel_expense_audit"
GENERATED_DIR = DATASET_ROOT / "generated"

FIXED_TIMESTAMP = datetime(2026, 11, 24, 0, 0, 0, tzinfo=UTC)
FIXED_ZIP_DATE_TIME = (2026, 11, 24, 0, 0, 0)
TOTAL_SIZE_BUDGET_BYTES = 4 * 1024 * 1024

# 标准 PDF 1.4 Standard Security Handler（Algorithm 3.2）固定 32 字节密码填充表。
_PDF_STD_PADDING = bytes(
    [
        0x28,
        0xBF,
        0x4E,
        0x5E,
        0x4E,
        0x75,
        0x8A,
        0x41,
        0x64,
        0x00,
        0x4E,
        0x56,
        0xFF,
        0xFA,
        0x01,
        0x08,
        0x2E,
        0x2E,
        0x00,
        0xB6,
        0xD0,
        0x68,
        0x3E,
        0x80,
        0x2F,
        0x0C,
        0xA9,
        0xFE,
        0x64,
        0x53,
        0x69,
        0x7A,
    ]
)

# 跨平台常见中文字体候选路径（macOS 与 Linux 容器按清晰度依次探测回退）。
CJK_FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# Markdown KB 源稿与生成目标映射（V1 5 档含 DOC-01 v2 + V2 4 档 + V3 5 档含 DOC-13 v2）。
MARKDOWN_KB_COPIES: tuple[tuple[str, str], ...] = (
    ("DOC-01_XC-HR-2026-001_v1.md", "kb/DOC-01_XC-HR-2026-001_假勤休假管理办法.md"),
    (
        "DOC-01_XC-HR-2026-001-REV2_v2.md",
        "kb/DOC-01_XC-HR-2026-001-REV2_假勤休假管理办法_v2.md",
    ),
    ("DOC-07_XC-HR-OPS-010.md", "kb/DOC-07_XC-HR-OPS-010_HR入转调离内部SOP.md"),
    ("DOC-11_XC-RD-ENG-030.md", "kb/DOC-11_XC-RD-ENG-030_研发权限与值班规范.md"),
    ("DOC-12_su_personal_notes.md", "kb/DOC-12_苏晚晴个人HR跟进备忘.md"),
    ("DOC-13_XC-HR-2026-007.md", "kb/DOC-13_XC-HR-2026-007_绩效考核与申诉制度.md"),
    (
        "DOC-13_XC-HR-2026-007-REV2_v2.md",
        "kb/DOC-13_XC-HR-2026-007-REV2_绩效考核与申诉制度_v2.md",
    ),
    ("DOC-16_XC-CMP-2026-010.md", "kb/DOC-16_XC-CMP-2026-010_员工申诉与合规举报指南.md"),
    ("DOC-20_XC-FIN-TRV-021.md", "kb/DOC-20_XC-FIN-TRV-021_差旅超标稽核与发票核验细则.md"),
    ("DOC-24_lu_personal_notes.md", "kb/DOC-24_陆知微个人研发架构笔记.md"),
    (
        "DOC-27_XC-HR-2025-001_archived.md",
        "kb/DOC-27_XC-HR-2025-001_假勤休假管理办法_2025已废止.md",
    ),
    ("DOC-34_XC-RD-ARCH-032.md", "kb/DOC-34_XC-RD-ARCH-032_生产变更封网与高危发布规范.md"),
    ("DOC-35_su_personal_notes_202612.md", "kb/DOC-35_苏晚晴2026-12跨年年假与面谈备忘.md"),
    ("DOC-36_lu_personal_notes_202612.md", "kb/DOC-36_陆知微2026-12封网值班与架构复盘笔记.md"),
)


def _load_cjk_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """探测系统已安装中文字体，避免在无指定字体的环境崩溃。"""
    for candidate in CJK_FONT_CANDIDATES:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


_CORE_XML_TS_PATTERN = re.compile(
    rb"(<dcterms:(?:created|modified)[^>]*>)[^<]+(</dcterms:(?:created|modified)>)"
)


def _normalize_zip_bytes(raw_zip: bytes) -> bytes:
    """重写 Office ZIP 成员时间戳与 core.xml 修改时间，确保跨时刻重复生成 100% 确定性。"""
    source = io.BytesIO(raw_zip)
    output = io.BytesIO()
    with zipfile.ZipFile(source, "r") as src_archive:
        members = sorted(src_archive.infolist(), key=lambda info: info.filename)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as dst_archive:
            for member in members:
                if member.is_dir():
                    continue
                data = src_archive.read(member.filename)
                if member.filename == "docProps/core.xml":
                    # openpyxl.save() 会在写盘瞬间覆写 modified 时间戳，此处统一锚定基准日。
                    data = _CORE_XML_TS_PATTERN.sub(rb"\g<1>2026-11-24T00:00:00Z\g<2>", data)
                clean_info = zipfile.ZipInfo(member.filename, date_time=FIXED_ZIP_DATE_TIME)
                clean_info.compress_type = zipfile.ZIP_DEFLATED
                clean_info.external_attr = 0o100644 << 16
                dst_archive.writestr(clean_info, data)
    return output.getvalue()


def _wrap_pdf_line(text: str, max_units: int = 76) -> list[str]:
    """按全角/半角近似显示宽度自动换行，保证 PDF 文本页排版规整。"""
    if not text:
        return [""]
    lines: list[str] = []
    current: list[str] = []
    units = 0
    for ch in text:
        char_units = 2 if ord(ch) > 127 else 1
        if current and units + char_units > max_units:
            lines.append("".join(current))
            current = [ch]
            units = char_units
        else:
            current.append(ch)
            units += char_units
    if current:
        lines.append("".join(current))
    return lines


def build_native_chinese_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    """构建纯原生中文可选中 PDF（Type0 CIDFontType2 + Identity-H + 分块 ToUnicode CMap）。

    设计要点：
    - 严禁嵌入整页位图，确保 ``LocalDocumentParser`` 不管是否开启 OCR 都直接由 pypdfium2 提取文本。
    - 遵循 Adobe CMap 规范，每段 ``beginbfchar`` 最多 100 条映射，避免超长段落导致解析器截断。
    """
    wrapped_pages: list[list[str]] = []
    for page_lines in pages:
        expanded: list[str] = []
        for raw_line in page_lines:
            expanded.extend(_wrap_pdf_line(raw_line))
        wrapped_pages.append(expanded)

    unique_chars = sorted({ch for page in wrapped_pages for line in page for ch in line})
    char_to_cid = {ch: index + 1 for index, ch in enumerate(unique_chars)}

    # 按 100 条分块写入 beginbfchar，确保大字符集时严格遵守 PDF CMap 规范。
    bf_entries = [f"<{cid:04X}> <{ord(ch):04X}>" for ch, cid in char_to_cid.items()]
    bf_blocks: list[str] = []
    for start in range(0, len(bf_entries), 100):
        chunk = bf_entries[start : start + 100]
        bf_blocks.append(f"{len(chunk)} beginbfchar\n" + "\n".join(chunk) + "\nendbfchar")

    cmap_bytes = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n"
        "<0000> <FFFF>\n"
        "endcodespacerange\n" + "\n".join(bf_blocks) + "\nendcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end\n"
        "end\n"
    ).encode("ascii")

    # 构造精确字符宽度数组（ASCII 半宽 500，非 ASCII 全宽 1000）。
    widths_tokens: list[str] = []
    for ch, cid in char_to_cid.items():
        width = 500 if ord(ch) <= 127 else 1000
        widths_tokens.append(f"{cid} [{width}]")
    widths_array = " ".join(widths_tokens)

    objects: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_id = add_obj(b"")
    pages_id = add_obj(b"")
    tounicode_id = add_obj(
        f"<< /Length {len(cmap_bytes)} >>\nstream\n".encode("ascii") + cmap_bytes + b"\nendstream"
    )
    font_desc_id = add_obj(
        b"<< /Type /FontDescriptor /FontName /STSong-Light /Flags 4 "
        b"/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 880 /Descent -120 "
        b"/CapHeight 750 /StemV 80 >>"
    )
    cidfont_id = add_obj(
        (
            f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /STSong-Light "
            f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            f"/FontDescriptor {font_desc_id} 0 R /DW 1000 /W [{widths_array}] >>"
        ).encode("ascii")
    )
    font_id = add_obj(
        (
            f"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /Identity-H "
            f"/DescendantFonts [{cidfont_id} 0 R] /ToUnicode {tounicode_id} 0 R >>"
        ).encode("ascii")
    )

    page_ids: list[int] = []
    for lines in wrapped_pages:
        ops = ["BT", "/F1 11 Tf", "48 785 Td", "18 TL"]
        for line_idx, line in enumerate(lines):
            hex_text = "".join(f"{char_to_cid[ch]:04X}" for ch in line)
            if line_idx == 0:
                ops.append(f"<{hex_text}> Tj")
            else:
                ops.append(f"T* <{hex_text}> Tj")
        ops.append("ET")
        stream_bytes = "\n".join(ops).encode("ascii")
        content_id = add_obj(
            f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream"
        )
        page_id = add_obj(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        page_ids.append(page_id)

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode(
        "ascii"
    )
    objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii")

    return _assemble_pdf(objects, catalog_id)


def build_scanned_no_text_pdf(spec: Mapping[str, Any]) -> bytes:
    """构建仅含单张 JPEG 图像 XObject、无任何文本层与字体对象的扫描件 PDF（用于验证空文本降级）。"""
    card_img = _render_card_image(
        header=str(spec["header"]),
        badge="仅图像扫描件",
        badge_color=(96, 125, 139),
        fields=cast(Sequence[Sequence[str]], spec["fields"]),
    )
    jpg_buf = io.BytesIO()
    card_img.convert("RGB").save(jpg_buf, format="JPEG", quality=82, optimize=True)
    jpg_bytes = jpg_buf.getvalue()
    width, height = card_img.size

    objects: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_id = add_obj(b"")
    pages_id = add_obj(b"")
    img_id = add_obj(
        (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
            f"/Length {len(jpg_bytes)} >>\nstream\n"
        ).encode("ascii")
        + jpg_bytes
        + b"\nendstream"
    )
    # 页面内容流仅绘制位图，严禁出现 BT..ET 文本算子。
    draw_stream = b"q\n595 0 0 420 0 210 cm\n/Im0 Do\nQ\n"
    content_id = add_obj(
        f"<< /Length {len(draw_stream)} >>\nstream\n".encode("ascii") + draw_stream + b"\nendstream"
    )
    page_id = add_obj(
        (
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /XObject << /Im0 {img_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
    )
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{page_id} 0 R] /Count 1 >>".encode("ascii")
    objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii")
    return _assemble_pdf(objects, catalog_id)


def build_multipage_scanned_no_text_pdf(spec: Mapping[str, Any]) -> bytes:
    """构建多页纯 JPEG 图像 XObject、无任何 BT..ET 文本层的扫描件 PDF（如 ATT-14 三页样本）。"""
    page_specs = cast(Sequence[Mapping[str, Any]], spec["pages"])
    objects: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_id = add_obj(b"")
    pages_id = add_obj(b"")
    page_ids: list[int] = []

    for idx, page_spec in enumerate(page_specs):
        raw_color = cast(Sequence[int], page_spec.get("badge_color", (96, 125, 139)))
        badge_color = (int(raw_color[0]), int(raw_color[1]), int(raw_color[2]))
        card_img = _render_card_image(
            header=str(page_spec["header"]),
            badge=str(page_spec.get("badge", "纯图扫描件")),
            badge_color=badge_color,
            fields=cast(Sequence[Sequence[str]], page_spec["fields"]),
        )
        jpg_buf = io.BytesIO()
        card_img.convert("RGB").save(jpg_buf, format="JPEG", quality=80, optimize=True)
        jpg_bytes = jpg_buf.getvalue()
        width, height = card_img.size

        img_name = f"Im{idx}"
        img_id = add_obj(
            (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
                f"/Length {len(jpg_bytes)} >>\nstream\n"
            ).encode("ascii")
            + jpg_bytes
            + b"\nendstream"
        )
        # 每页仅绘制位图 XObject，严禁出现任何文本绘制算子（BT..ET）与字体资源。
        draw_stream = f"q\n595 0 0 420 0 210 cm\n/{img_name} Do\nQ\n".encode("ascii")
        content_id = add_obj(
            f"<< /Length {len(draw_stream)} >>\nstream\n".encode("ascii")
            + draw_stream
            + b"\nendstream"
        )
        page_id = add_obj(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /XObject << /{img_name} {img_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        page_ids.append(page_id)

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode(
        "ascii"
    )
    objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii")
    return _assemble_pdf(objects, catalog_id)


def _rc4_crypt(key: bytes, data: bytes) -> bytes:
    """标准 RC4 流加密，用于构造符合 PDF 1.4 Standard Security Handler 的测试密件。"""
    state = list(range(256))
    j = 0
    key_len = len(key)
    for i in range(256):
        j = (j + state[i] + key[i % key_len]) & 0xFF
        state[i], state[j] = state[j], state[i]
    i = 0
    j = 0
    out = bytearray(len(data))
    for idx, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        out[idx] = byte ^ state[(state[i] + state[j]) & 0xFF]
    return bytes(out)


def build_encrypted_pdf_bytes(spec: Mapping[str, Any]) -> bytes:
    """构建标准 PDF 1.4 /Encrypt 密码保护 PDF（确定性无随机盐，真实触发 FPDF_ERR_PASSWORD）。"""
    raw_user_pw = str(spec["user_password"]).encode("latin-1")
    raw_owner_pw = str(spec.get("owner_password", spec["user_password"])).encode("latin-1")
    padded_user = (raw_user_pw + _PDF_STD_PADDING)[:32]
    padded_owner = (raw_owner_pw + _PDF_STD_PADDING)[:32]

    # 按 PDF 1.4 Algorithm 3.3 / 3.2 / 3.4 派生 /O、加密密钥与 /U（V=1, R=2, 40-bit）。
    owner_key = hashlib.md5(padded_owner).digest()[:5]
    o_val = _rc4_crypt(owner_key, padded_user)
    file_id = hashlib.md5(str(spec["file_id_seed"]).encode("utf-8")).digest()
    p_int = -4  # 允许常规浏览权限位，但必须提供正确用户密码才可打开。
    p_bytes = (p_int & 0xFFFFFFFF).to_bytes(4, "little")
    enc_key = hashlib.md5(padded_user + o_val + p_bytes + file_id).digest()[:5]
    u_val = _rc4_crypt(enc_key, _PDF_STD_PADDING)

    # 对第 4 号页面内容流对象执行标准对象密钥（enc_key + obj_num + gen_num）RC4 加密。
    plain_text = str(spec.get("content_text", "Confidential")).replace("(", "[").replace(")", "]")
    plain_stream = f"BT /F1 12 Tf 72 720 Td ({plain_text}) Tj ET".encode("latin-1")
    obj_key = hashlib.md5(enc_key + (4).to_bytes(3, "little") + (0).to_bytes(2, "little")).digest()[
        :10
    ]
    encrypted_stream = _rc4_crypt(obj_key, plain_stream)

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >>",
        (
            f"<< /Length {len(encrypted_stream)} >>\nstream\n".encode("ascii")
            + encrypted_stream
            + b"\nendstream"
        ),
        (
            f"<< /Filter /Standard /V 1 /R 2 /Length 40 /P {p_int} "
            f"/O <{o_val.hex().upper()}> /U <{u_val.hex().upper()}> >>"
        ).encode("ascii"),
    ]
    return _assemble_pdf(objects, catalog_id=1, encrypt_id=5, file_id=file_id)


def _assemble_pdf(
    objects: Sequence[bytes],
    catalog_id: int,
    *,
    encrypt_id: int | None = None,
    file_id: bytes | None = None,
) -> bytes:
    """按标准 PDF 1.4 交叉引用表格式拼装确定性 PDF 字节流。"""
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for obj_id, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{obj_id} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_pos = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    extra_trailer = ""
    if encrypt_id is not None:
        extra_trailer += f" /Encrypt {encrypt_id} 0 R"
    if file_id is not None:
        id_hex = file_id.hex().upper()
        extra_trailer += f" /ID [<{id_hex}> <{id_hex}>]"
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R{extra_trailer} >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def build_macro_docx_bytes(spec: Mapping[str, Any]) -> bytes:
    """构建含确定性 word/vbaProject.bin 的异常 DOCX 样本，触发 document_macro_unsupported。"""
    base_docx = build_docx_bytes(spec)
    source = io.BytesIO(base_docx)
    output = io.BytesIO()
    # 构造无害固定字节流模拟 VBA 宏二进制载体（OLE2 魔数头 + 明确测试声明），绝不含可执行代码。
    vba_stub = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1ATHENA_V2_MOCK_VBA_MACRO_BOUNDARY_SAMPLE\x00"
    with zipfile.ZipFile(source, "r") as src_archive:
        entries: dict[str, bytes] = {
            member.filename: src_archive.read(member.filename)
            for member in src_archive.infolist()
            if not member.is_dir()
        }
    entries["word/vbaProject.bin"] = vba_stub
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as dst_archive:
        for filename in sorted(entries):
            info = zipfile.ZipInfo(filename, date_time=FIXED_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            dst_archive.writestr(info, entries[filename])
    return output.getvalue()


def build_docx_bytes(spec: Mapping[str, Any]) -> bytes:
    """构建含标题、正文段落与真实多行表格的确定性 DOCX 文档。"""
    doc = DocxDocument()
    doc.core_properties.author = "星辰智联科技有限公司"
    doc.core_properties.title = str(spec["title"])
    doc.core_properties.created = FIXED_TIMESTAMP.replace(tzinfo=None)
    doc.core_properties.modified = FIXED_TIMESTAMP.replace(tzinfo=None)

    doc.add_heading(str(spec["title"]), level=1)
    for section in cast(Sequence[Mapping[str, Any]], spec["sections"]):
        doc.add_heading(str(section["heading"]), level=2)
        for paragraph in cast(Sequence[str], section.get("paragraphs", ())):
            doc.add_paragraph(str(paragraph))
        table_spec = section.get("table")
        if isinstance(table_spec, Mapping):
            headers = [str(item) for item in cast(Sequence[Any], table_spec["headers"])]
            rows = cast(Sequence[Sequence[Any]], table_spec["rows"])
            table = doc.add_table(rows=1 + len(rows), cols=len(headers))
            for col_idx, header in enumerate(headers):
                table.rows[0].cells[col_idx].text = header
            for row_idx, row_values in enumerate(rows, start=1):
                for col_idx, cell_value in enumerate(row_values):
                    table.rows[row_idx].cells[col_idx].text = str(cell_value)

    buf = io.BytesIO()
    doc.save(buf)
    return _normalize_zip_bytes(buf.getvalue())


def build_xlsx_bytes(spec: Mapping[str, Any]) -> bytes:
    """构建多 Sheet、纯静态明确值（无未缓存公式）的确定性 XLSX 工作簿。"""
    workbook = Workbook()
    workbook.properties.creator = "星辰智联科技有限公司"
    workbook.properties.lastModifiedBy = "星辰智联科技有限公司"
    workbook.properties.created = FIXED_TIMESTAMP.replace(tzinfo=None)
    workbook.properties.modified = FIXED_TIMESTAMP.replace(tzinfo=None)

    header_fill = PatternFill("solid", fgColor="1B4965")
    header_font = Font(color="FFFFFF", bold=True)

    sheets = cast(Sequence[Mapping[str, Any]], spec["sheets"])
    for index, sheet_spec in enumerate(sheets):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        sheet.title = str(sheet_spec["name"])
        headers = [str(item) for item in cast(Sequence[Any], sheet_spec["headers"])]
        sheet.append(headers)
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row_values in cast(Sequence[Sequence[Any]], sheet_spec["rows"]):
            sheet.append(list(row_values))
        for col_idx, header in enumerate(headers, start=1):
            col_letter = sheet.cell(1, col_idx).column_letter
            sheet.column_dimensions[col_letter].width = max(14, min(48, len(header) * 2 + 8))
        sheet.freeze_panes = "A2"

    buf = io.BytesIO()
    workbook.save(buf)
    return _normalize_zip_bytes(buf.getvalue())


def build_pptx_bytes(spec: Mapping[str, Any]) -> bytes:
    """构建包含标题、正文文本框与原生表格的多页确定性 PPTX 演示文稿。"""
    prs = Presentation()
    prs.core_properties.author = "星辰智联科技有限公司"
    prs.core_properties.created = FIXED_TIMESTAMP.replace(tzinfo=None)
    prs.core_properties.modified = FIXED_TIMESTAMP.replace(tzinfo=None)

    blank_layout = prs.slide_layouts[6]
    for slide_spec in cast(Sequence[Mapping[str, Any]], spec["slides"]):
        slide = prs.slides.add_slide(blank_layout)
        title_box = slide.shapes.add_textbox(Inches(0.6), Inches(0.4), Inches(8.8), Inches(0.8))
        title_frame = title_box.text_frame
        title_frame.word_wrap = True
        title_para = title_frame.paragraphs[0]
        title_para.text = str(slide_spec["title"])
        title_para.font.size = Pt(24)
        title_para.font.bold = True

        body_lines = [str(item) for item in cast(Sequence[Any], slide_spec.get("body", ()))]
        table_spec = slide_spec.get("table")
        body_height = Inches(1.4) if isinstance(table_spec, Mapping) else Inches(5.2)
        body_box = slide.shapes.add_textbox(Inches(0.6), Inches(1.3), Inches(8.8), body_height)
        body_frame = body_box.text_frame
        body_frame.word_wrap = True
        for line_idx, line in enumerate(body_lines):
            para = body_frame.paragraphs[0] if line_idx == 0 else body_frame.add_paragraph()
            para.text = line
            para.font.size = Pt(14)

        if isinstance(table_spec, Mapping):
            headers = [str(item) for item in cast(Sequence[Any], table_spec["headers"])]
            rows = cast(Sequence[Sequence[Any]], table_spec["rows"])
            table_shape = slide.shapes.add_table(
                1 + len(rows),
                len(headers),
                Inches(0.6),
                Inches(2.9),
                Inches(8.8),
                Inches(3.6),
            )
            table = table_shape.table
            for col_idx, header in enumerate(headers):
                cell = table.cell(0, col_idx)
                cell.text = header
                cell.text_frame.paragraphs[0].font.size = Pt(12)
                cell.text_frame.paragraphs[0].font.bold = True
            for row_idx, row_values in enumerate(rows, start=1):
                for col_idx, value in enumerate(row_values):
                    cell = table.cell(row_idx, col_idx)
                    cell.text = str(value)
                    cell.text_frame.paragraphs[0].font.size = Pt(11)

    buf = io.BytesIO()
    prs.save(buf)
    return _normalize_zip_bytes(buf.getvalue())


def _render_flowchart_image(spec: Mapping[str, Any]) -> Image.Image:
    """绘制高对比度双栏中文业务流程图，适合视觉模型与 OCR 识别。"""
    width, height = 1120, 680
    image = Image.new("RGB", (width, height), (250, 252, 255))
    draw = ImageDraw.Draw(image)
    title_font = _load_cjk_font(24)
    subtitle_font = _load_cjk_font(15)
    header_font = _load_cjk_font(18)
    step_font = _load_cjk_font(15)

    # 顶部品牌深蓝标题栏。
    draw.rectangle([(0, 0), (width, 92)], fill=(27, 73, 101))
    draw.text((32, 18), str(spec["title"]), fill=(255, 255, 255), font=title_font)
    draw.text((32, 56), str(spec.get("subtitle", "")), fill=(214, 232, 245), font=subtitle_font)

    columns = cast(Sequence[Mapping[str, Any]], spec["columns"])
    col_width = 508
    for col_idx, col in enumerate(columns):
        x0 = 32 + col_idx * (col_width + 40)
        x1 = x0 + col_width
        draw.rounded_rectangle(
            [(x0, 114), (x1, 158)],
            radius=8,
            fill=(227, 242, 253),
            outline=(27, 73, 101),
            width=2,
        )
        draw.text((x0 + 16, 125), str(col["header"]), fill=(15, 44, 66), font=header_font)

        steps = [str(item) for item in cast(Sequence[Any], col["steps"])]
        y_cursor = 176
        box_height = 74
        for step_idx, step_text in enumerate(steps):
            draw.rounded_rectangle(
                [(x0, y_cursor), (x1, y_cursor + box_height)],
                radius=8,
                fill=(255, 255, 255),
                outline=(70, 118, 155),
                width=2,
            )
            wrapped = "\n".join(textwrap.wrap(step_text, width=30))
            draw.multiline_text(
                (x0 + 16, y_cursor + 14),
                wrapped,
                fill=(24, 34, 48),
                font=step_font,
                spacing=6,
            )
            if step_idx < len(steps) - 1:
                mid_x = (x0 + x1) // 2
                draw.line(
                    [(mid_x, y_cursor + box_height), (mid_x, y_cursor + box_height + 20)],
                    fill=(27, 73, 101),
                    width=3,
                )
                draw.polygon(
                    [
                        (mid_x - 6, y_cursor + box_height + 14),
                        (mid_x + 6, y_cursor + box_height + 14),
                        (mid_x, y_cursor + box_height + 22),
                    ],
                    fill=(27, 73, 101),
                )
            y_cursor += box_height + 24

    return image


def _render_card_image(
    *,
    header: str,
    badge: str,
    badge_color: tuple[int, int, int],
    fields: Sequence[Sequence[str]],
) -> Image.Image:
    """绘制清晰高对比度的 OA 截图、门诊病假证明或 IT 签收单视觉样张。"""
    width, height = 1040, 620
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    title_font = _load_cjk_font(22)
    badge_font = _load_cjk_font(16)
    key_font = _load_cjk_font(16)
    val_font = _load_cjk_font(16)

    draw.rectangle([(0, 0), (width, 84)], fill=(27, 73, 101))
    draw.text((28, 26), header, fill=(255, 255, 255), font=title_font)

    # 右上角醒目标签或模拟印章。
    draw.rounded_rectangle([(width - 178, 22), (width - 24, 62)], radius=18, fill=badge_color)
    draw.text((width - 160, 32), badge, fill=(255, 255, 255), font=badge_font)

    y = 108
    row_height = 76
    for idx, pair in enumerate(fields):
        label, value = str(pair[0]), str(pair[1])
        bg = (246, 249, 252) if idx % 2 == 0 else (255, 255, 255)
        draw.rectangle(
            [(28, y), (width - 28, y + row_height - 8)],
            fill=bg,
            outline=(212, 222, 232),
        )
        draw.text((44, y + 20), label, fill=(40, 66, 92), font=key_font)
        wrapped_val = "\n".join(textwrap.wrap(value, width=42))
        draw.multiline_text((260, y + 14), wrapped_val, fill=(20, 26, 36), font=val_font, spacing=5)
        y += row_height

    return image


def build_png_bytes(image: Image.Image) -> bytes:
    """以确定性优化参数编码 PNG 字节流。"""
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_webp_bytes(image: Image.Image) -> bytes:
    """以确定性参数编码真实 WebP 图像字节流（供多模态 WebP 样本与视觉归一化验证）。"""
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="WEBP", quality=82, method=4)
    return buf.getvalue()


def build_jpg_with_exif_bytes(spec: Mapping[str, Any]) -> bytes:
    """绘制设备签收单拍照件并写入无害模拟 EXIF（含 Make/Model/Software/GPSInfo）。"""
    image = _render_card_image(
        header=str(spec["header"]),
        badge="拍照归档件",
        badge_color=(46, 125, 50),
        fields=cast(Sequence[Sequence[str]], spec["fields"]),
    )
    exif_spec = cast(Mapping[str, Any], spec["exif"])
    exif = Image.Exif()
    exif[Base.Make] = str(exif_spec["make"])
    exif[Base.Model] = str(exif_spec["model"])
    exif[Base.Software] = str(exif_spec["software"])
    exif[Base.Orientation] = 1
    lat_vals = tuple(float(v) for v in cast(Sequence[Any], exif_spec["gps_lat"]))
    lon_vals = tuple(float(v) for v in cast(Sequence[Any], exif_spec["gps_lon"]))
    # 写入标准 GPSInfo 子 IFD（0x8825），用于测试图片归一化器是否完整剥离隐私定位数据。
    exif[0x8825] = {
        GPS.GPSLatitudeRef: str(exif_spec["gps_lat_ref"]),
        GPS.GPSLatitude: lat_vals,
        GPS.GPSLongitudeRef: str(exif_spec["gps_lon_ref"]),
        GPS.GPSLongitude: lon_vals,
    }
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=86, optimize=True, exif=exif)
    return buf.getvalue()


def build_skill_zip_bytes(skill_source_dir: Path) -> bytes:
    """以固定时间戳与顺序将 Skill（如 SK-5、SK-7）源目录打成 Athena 标准 ZIP。"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(skill_source_dir.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(skill_source_dir)
            if "__pycache__" in rel.parts or file_path.suffix in {".pyc", ".pyo"}:
                continue
            info = zipfile.ZipInfo(rel.as_posix(), date_time=FIXED_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, file_path.read_bytes())
    return output.getvalue()


def _render_asset_from_spec(spec: Mapping[str, Any]) -> bytes:
    """根据 structured_docs.json 中声明的格式分发至对应确定性构建器。"""
    fmt = str(spec["format"])
    if fmt == "docx":
        return build_docx_bytes(spec)
    if fmt == "docx_with_macro":
        return build_macro_docx_bytes(spec)
    if fmt == "pdf":
        return build_native_chinese_pdf(cast(Sequence[Sequence[str]], spec["pages"]))
    if fmt == "pdf_scanned_no_text":
        return build_scanned_no_text_pdf(spec)
    if fmt == "pdf_scanned_no_text_multipage":
        return build_multipage_scanned_no_text_pdf(spec)
    if fmt == "pdf_encrypted":
        return build_encrypted_pdf_bytes(spec)
    if fmt == "xlsx":
        return build_xlsx_bytes(spec)
    if fmt == "pptx":
        return build_pptx_bytes(spec)
    if fmt == "png_flowchart":
        return build_png_bytes(_render_flowchart_image(spec))
    if fmt == "png_card":
        raw_color = cast(Sequence[int], spec.get("badge_color", (27, 73, 101)))
        color = (int(raw_color[0]), int(raw_color[1]), int(raw_color[2]))
        return build_png_bytes(
            _render_card_image(
                header=str(spec["header"]),
                badge=str(spec.get("badge", "样例")),
                badge_color=color,
                fields=cast(Sequence[Sequence[str]], spec["fields"]),
            )
        )
    if fmt == "webp_card":
        raw_color = cast(Sequence[int], spec.get("badge_color", (46, 125, 50)))
        color = (int(raw_color[0]), int(raw_color[1]), int(raw_color[2]))
        return build_webp_bytes(
            _render_card_image(
                header=str(spec["header"]),
                badge=str(spec.get("badge", "WebP样本")),
                badge_color=color,
                fields=cast(Sequence[Sequence[str]], spec["fields"]),
            )
        )
    if fmt == "jpg_exif_receipt":
        return build_jpg_with_exif_bytes(spec)
    if fmt == "corrupt_raw_bytes":
        # 固定伪装损坏图片负载，用于验证视觉归一化器与 Run 单图容错边界。
        return str(spec["payload_ascii"]).encode("ascii")
    raise ValueError(f"unsupported_asset_format:{fmt}")


def build_all_assets(output_root: Path = GENERATED_DIR) -> dict[str, object]:
    """全量生成企业测试资产、校验轻量化总大小预算并落盘 checksums.json。"""
    output_root.mkdir(parents=True, exist_ok=True)
    structured = json.loads((KB_SOURCES_DIR / "structured_docs.json").read_text(encoding="utf-8"))

    written_files: dict[str, dict[str, object]] = {}

    def write_binary(relpath: str, payload: bytes) -> None:
        target = output_root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        written_files[relpath] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }

    # 1. 规范化复制全部 Markdown KB 样本（统一 UTF-8 + LF 换行）。
    for src_name, dst_rel in MARKDOWN_KB_COPIES:
        text = (KB_SOURCES_DIR / src_name).read_text(encoding="utf-8").replace("\r\n", "\n")
        write_binary(dst_rel, text.encode("utf-8"))

    # 2. 生成全部二进制 KB 文档与多模态会话附件。
    for group_key in ("kb_documents", "attachments"):
        group = cast(Mapping[str, Mapping[str, Any]], structured[group_key])
        for asset_id in sorted(group):
            spec = group[asset_id]
            write_binary(str(spec["output_relpath"]), _render_asset_from_spec(spec))

    # 3. 确定性打包 SK-5 与 SK-7 两套 ZIP 脚本技能。
    skill_zip_bytes = build_skill_zip_bytes(SKILL_SOURCE_DIR)
    write_binary("skill_packages/hroa-lifecycle-audit-pack.zip", skill_zip_bytes)

    travel_skill_zip_bytes = build_skill_zip_bytes(TRAVEL_SKILL_SOURCE_DIR)
    write_binary("skill_packages/travel-expense-audit-pack.zip", travel_skill_zip_bytes)

    total_bytes = sum(int(cast(int, meta["size_bytes"])) for meta in written_files.values())
    if total_bytes > TOTAL_SIZE_BUDGET_BYTES:
        raise ValueError(
            f"generated_assets_exceed_budget: {total_bytes} > {TOTAL_SIZE_BUDGET_BYTES}"
        )

    checksums_payload: dict[str, object] = {
        "schema_version": 1,
        "baseline_date": "2026-11-24",
        "file_count": len(written_files),
        "total_bytes": total_bytes,
        "files": dict(sorted(written_files.items())),
    }
    checksums_path = output_root / "checksums.json"
    checksums_path.write_text(
        json.dumps(checksums_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return checksums_payload


def main() -> None:
    """CLI 入口：重建全部确定性测试资产并打印体积汇总。"""
    summary = build_all_assets(GENERATED_DIR)
    print(
        json.dumps(
            {
                "status": "ok",
                "output_dir": str(GENERATED_DIR),
                "file_count": summary["file_count"],
                "total_bytes": summary["total_bytes"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
