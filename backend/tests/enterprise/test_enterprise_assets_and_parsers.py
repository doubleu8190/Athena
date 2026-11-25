"""企业场景测试资产自洽性、多格式本地解析、无文本 PDF 降级与 EXIF 隐私剥离测试。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]
from PIL import Image
from PIL.ExifTags import Base

from app.org.mock_seed import MOCK_DEPARTMENTS, MOCK_USER_SPECS
from domain.file import (
    SessionDocumentExtractionResult,
    SessionFileExtractionStatus,
    VisionImageError,
)
from infrastructure.file.document_extractor import LocalSessionDocumentExtractor
from infrastructure.file.image_normalizer import LocalVisionImageNormalizer
from infrastructure.knowledge.parsers import LocalDocumentParser
from mcp_servers.hroa.data import DEPARTMENTS as HROA_DEPARTMENTS
from mcp_servers.hroa.data import EMPLOYEES as HROA_EMPLOYEES

REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_ROOT = REPO_ROOT / "datasets" / "enterprise_hroa"
GENERATED_DIR = DATASET_ROOT / "generated"
MANIFEST_PATH = DATASET_ROOT / "manifest.yaml"
CHECKSUMS_PATH = GENERATED_DIR / "checksums.json"


def _load_manifest() -> dict[str, Any]:
    """读取企业场景单一声明清单 manifest.yaml。"""
    return cast(dict[str, Any], yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")))


class _ForbiddenOcrEngine:
    """哨兵 OCR 引擎：用于证明纯原生中文 PDF 在开启 OCR 时也绝不触发位图 OCR。"""

    def predict(self, **kwargs: object) -> list[object]:
        raise AssertionError(f"native_pdf_must_not_trigger_ocr:{kwargs.keys()}")


def test_manifest_worldview_and_checksums_are_consistent() -> None:
    """校验 manifest.yaml 与 mock_seed、HROA 基线花名册及 checksums.json 100% 自洽。"""
    manifest = _load_manifest()
    assert manifest["schema_version"] == 1
    worldview = cast(dict[str, Any], manifest["worldview"])
    assert worldview["company_name"] == "星辰智联科技有限公司"
    assert worldview["baseline_date"] == "2026-11-24"
    assert worldview["monthly_payroll_days"] == 21.75
    assert worldview["min_leave_step_days"] == 0.5

    # 校验 5 个部门 ID 与 mock_seed.py 及 HROA MCP 基线严格一致。
    mock_dept_ids = {dept.id for dept in MOCK_DEPARTMENTS}
    manifest_dept_ids = {item["id"] for item in worldview["departments"]}
    hroa_dept_ids = {str(item["department_id"]) for item in HROA_DEPARTMENTS}
    assert manifest_dept_ids == mock_dept_ids == hroa_dept_ids

    # 校验 6 个 Mock 账号 ID / 工号与 14 人统一花名册一致。
    mock_pairs = {(spec[0], spec[4]) for spec in MOCK_USER_SPECS}
    manifest_mock_pairs = {
        (item["user_id"], item["employee_no"]) for item in worldview["mock_accounts"]
    }
    assert manifest_mock_pairs == mock_pairs

    hroa_emp_nos = {str(item["employee_no"]) for item in HROA_EMPLOYEES}
    expected_emp_nos = {item["employee_no"] for item in worldview["mock_accounts"]} | {
        item["employee_no"] for item in worldview["core_subjects"]
    }
    assert len(hroa_emp_nos) == 14
    assert hroa_emp_nos == expected_emp_nos

    # 校验 V3 规模：6 库 36 档（含 2 份版本替换样张）、6 记忆角色、3 MCP（16 工具）、
    # 9 技能、3 Subagents、2 项目、19 附件。
    kbs = cast(list[dict[str, Any]], manifest["knowledge_bases"])
    assert len(kbs) == 6
    all_kb_docs = [doc for kb in kbs for doc in cast(list[dict[str, Any]], kb["documents"])]
    assert len(all_kb_docs) == 36
    revision_samples = [doc["revision_sample"] for doc in all_kb_docs if doc.get("revision_sample")]
    assert len(revision_samples) == 2
    assert {str(r["doc_id"]) for r in revision_samples} == {"DOC-01-V2", "DOC-13-V2"}
    assert len(manifest["preferences_and_memories"]) == 6
    assert len(manifest["mcp_servers"]) == 3
    assert sum(len(s["tools"]) for s in manifest["mcp_servers"]) == 16
    assert len(manifest["skills"]) == 9
    assert len(manifest["subagents"]) == 3
    assert len(manifest["projects"]) == 2
    assert len(manifest["attachments"]) == 19

    # 校验全部非个人知识库文档的 policy_code 均已登记在 worldview.policy_codes 中。
    registered_policy_codes = set(worldview["policy_codes"]["company"]) | set(
        worldview["policy_codes"]["department"]
    )
    for kb in kbs:
        if kb["visibility_scope"] == "personal":
            continue
        for doc in cast(list[dict[str, Any]], kb["documents"]):
            code = str(doc["policy_code"])
            assert code in registered_policy_codes, (
                f"unregistered_policy_code doc={doc['doc_id']} code={code}"
            )

    # 校验全部 59 份生成文件 SHA-256 与轻量化总大小预算（< 4MB）。
    checksums = json.loads(CHECKSUMS_PATH.read_text(encoding="utf-8"))
    assert checksums["file_count"] == 59
    assert checksums["total_bytes"] < 4 * 1024 * 1024
    recorded_files = cast(dict[str, dict[str, Any]], checksums["files"])
    assert len(recorded_files) == checksums["file_count"]
    for relpath, meta in recorded_files.items():
        disk_path = GENERATED_DIR / relpath
        assert disk_path.is_file(), f"missing_generated_file:{relpath}"
        raw = disk_path.read_bytes()
        assert len(raw) == meta["size_bytes"]
        assert hashlib.sha256(raw).hexdigest() == meta["sha256"]


def test_all_kb_and_valid_attachment_files_pass_local_validator() -> None:
    """36 份 KB、2 份版本替换样张与 15 份兼容文档解析器的常规附件须通过校验。"""
    manifest = _load_manifest()
    validator = LocalDocumentParser(enable_ocr=False)

    paths: list[Path] = []
    for kb in cast(list[dict[str, Any]], manifest["knowledge_bases"]):
        for doc in cast(list[dict[str, Any]], kb["documents"]):
            paths.append(GENERATED_DIR / str(doc["file"]))
            rev = doc.get("revision_sample")
            if isinstance(rev, dict):
                paths.append(GENERATED_DIR / str(rev["file"]))
    for att in cast(list[dict[str, Any]], manifest["attachments"]):
        # ATT-10(加密PDF)、ATT-11(含宏DOCX)、ATT-19(伪装损坏PNG)为安全拒绝负样本；
        # ATT-15(WebP)为会话视觉图片，交由 LocalVisionImageNormalizer 专项校验。
        if (
            att.get("expected_extraction_error")
            in {
                "document_encrypted",
                "document_macro_unsupported",
                "vision_image_invalid",
            }
            or att.get("format") == "webp"
        ):
            continue
        paths.append(GENERATED_DIR / str(att["file"]))

    # 36 KB + 2 版本替换样张 + 15 常规/无文本PDF附件 = 53 份。
    assert len(paths) == 53
    for file_path in paths:
        assert file_path.is_file(), f"file_not_found:{file_path}"
        validator.validate_file(file_path.name, file_path)


@pytest.mark.parametrize(
    ("relpath", "expected_keywords", "expected_locator_kinds"),
    [
        (
            "kb/DOC-01_XC-HR-2026-001_假勤休假管理办法.md",
            ("XC-HR-2026-001", "09:30–18:30", "21.75", "0.5", "育儿假", "5"),
            {"markdown"},
        ),
        (
            "kb/DOC-01_XC-HR-2026-001-REV2_假勤休假管理办法_v2.md",
            ("XC-HR-2026-001-REV2", "2026-12-01", "10", "育儿假"),
            {"markdown"},
        ),
        (
            "kb/DOC-02_XC-HR-2026-002_入职与试用期管理规范.docx",
            ("XC-HR-2026-002", "OFFER-2026-1108", "15 个工作日", "MacBook Pro 14"),
            {"docx", "docx_table"},
        ),
        (
            "kb/DOC-03_XC-HR-2026-003_离职交接与清算管理制度.pdf",
            ("XC-HR-2026-003", "work_handover", "it_asset", "finance_advance", "200%", "18:00"),
            {"pdf"},
        ),
        (
            "kb/DOC-04_XC-HR-2026-004_调岗晋升与异动管理办法.pptx",
            ("XC-HR-2026-004", "100% 全额结转", "P5→P6", "3 个工作日"),
            {"pptx", "pptx_table"},
        ),
        (
            "kb/DOC-05_XC-ADM-FIN-2026_设备与差旅标准表.xlsx",
            ("XC-ADM-2026-005", "XC-FIN-2026-006", "16999", "650", "450", "5,000.00"),
            {"xlsx"},
        ),
        (
            "kb/DOC-07_XC-HR-OPS-010_HR入转调离内部SOP.md",
            ("XC-HR-OPS-010", "hroa-lifecycle-audit-pack", "XC1006", "IT-MBP-2023-088"),
            {"markdown"},
        ),
        (
            "kb/DOC-08_2026-11_HR入转调离跟踪台账.xlsx",
            ("XC1001", "XC1002", "XC1003", "XC1004", "XC1005", "XC1006", "XC1007", "XC1008"),
            {"xlsx"},
        ),
        (
            "kb/DOC-09_XC-FIN-INT-020_财务离职清算稽核细则.docx",
            ("XC-FIN-INT-020", "FIN-ADV-2026-0914", "2,400.00", "21.75", "T-3"),
            {"docx", "docx_table"},
        ),
        (
            "kb/DOC-10_2026Q4_财务HR协同结算指南.pdf",
            ("25 日", "28 日", "次月 10 日", "林清岚", "XC1003", "XC1004"),
            {"pdf"},
        ),
        (
            "kb/DOC-11_XC-RD-ENG-030_研发权限与值班规范.md",
            ("XC-RD-ENG-030", "MOCK005", "XC1001", "1.5", "18:00", "kubeconfig"),
            {"markdown"},
        ),
        (
            "kb/DOC-12_苏晚晴个人HR跟进备忘.md",
            ("HRBP-PRIVATE-NOTE-202611", "IT-MBP-2023-088", "FIN-ADV-2026-0914", "XC1005"),
            {"markdown"},
        ),
        (
            "kb/DOC-13_XC-HR-2026-007_绩效考核与申诉制度.md",
            ("XC-HR-2026-007", "2026-11-20", "2026-12-05", "60 天", "5 个工作日", "2026-12-02"),
            {"markdown"},
        ),
        (
            "kb/DOC-13_XC-HR-2026-007-REV2_绩效考核与申诉制度_v2.md",
            ("XC-HR-2026-007-REV2", "7 个工作日", "2026-12-04", "60 天"),
            {"markdown"},
        ),
        (
            "kb/DOC-14_XC-LEG-2026-008_合同审批与用印管理规范.docx",
            ("XC-LEG-2026-008", "CTR-2026-1120", "360,000.00", "LEG-REV-2026-0418", "周明远"),
            {"docx", "docx_table"},
        ),
        (
            "kb/DOC-15_XC-IT-2026-009_IT工单服务与权限SLA规范.pdf",
            ("XC-IT-2026-009", "15 分钟", "4 小时", "3 个工作日", "18:00"),
            {"pdf"},
        ),
        (
            "kb/DOC-16_XC-CMP-2026-010_员工申诉与合规举报指南.md",
            ("XC-CMP-2026-010", "integrity@xingchen.local", "7 个工作日", "反报复"),
            {"markdown"},
        ),
        (
            "kb/DOC-17_XC-HR-PERF-011_2026Q4绩效校准操作手册.pptx",
            ("XC-HR-PERF-011", "XC1004", "XC1007", "XC1008", "60 天 PIP"),
            {"pptx", "pptx_table"},
        ),
        (
            "kb/DOC-18_2026Q4_全员绩效初评与申诉跟踪台账.xlsx",
            ("PERF-TRK-2026-01", "XC1008", "not_applicable_probation", "archived_resigned"),
            {"xlsx"},
        ),
        (
            "kb/DOC-19_新员工背调核验与录用风控指引.pdf",
            ("XC-HR-BG-012", "OFFER-2026-1108", "竞业限制", "一票否决"),
            {"pdf"},
        ),
        (
            "kb/DOC-20_XC-FIN-TRV-021_差旅超标稽核与发票核验细则.md",
            ("XC-FIN-TRV-021", "travel-expense-audit-pack", "540.00", "300.00", "T-5"),
            {"markdown"},
        ),
        (
            "kb/DOC-21_2026-11_差旅报销与合同付款待审台账.xlsx",
            ("TRV-2026-1101", "TRV-2026-1102", "TRV-2026-1103", "TRV-2026-1104", "CTR-2026-1120"),
            {"xlsx"},
        ),
        (
            "kb/DOC-22_XC-RD-ONCALL-031_研发值班补贴与故障复盘规范.docx",
            ("XC-RD-ONCALL-031", "300元/次", "48 小时", "CAPA"),
            {"docx", "docx_table"},
        ),
        (
            "kb/DOC-23_2026-11-12_研发部OnCall值班排班表.xlsx",
            ("2026-W48", "2026-W51", "ONC-2026-1101", "陆知微"),
            {"xlsx"},
        ),
        (
            "kb/DOC-24_陆知微个人研发架构笔记.md",
            ("LU-PERSONAL-ARCH-202611", "athena-core", "helm rollback", "陈思远"),
            {"markdown"},
        ),
        (
            "kb/DOC-25_XC-SEC-2026-011_信息安全与数据分级规范.pdf",
            ("XC-SEC-2026-011", "L1", "L4", "2 小时", "sec-ops@xingchen.local", "180"),
            {"pdf"},
        ),
        (
            "kb/DOC-26_XC-PRC-2026-012_供应商准入与采购审批制度.docx",
            ("XC-PRC-2026-012", "50,000.00", "300,000.00", "拆分订单", "30 个自然日"),
            {"docx", "docx_table"},
        ),
        (
            "kb/DOC-27_XC-HR-2025-001_假勤休假管理办法_2025已废止.md",
            ("XC-HR-2025-001", "2026-01-01", "XC-HR-2026-001", "1.0", "3"),
            {"markdown"},
        ),
        (
            "kb/DOC-28_XC-ADM-2026-013_三地办公与异地派遣指引.pptx",
            ("XC-ADM-2026-013", "7%", "12%", "10%", "6,500.00", "10:00–19:00"),
            {"pptx", "pptx_table"},
        ),
        (
            "kb/DOC-30_2026-12_跨年入转调离与年假结转总台账.xlsx",
            ("XC1001", "probation", "XC1002", "active", "XC1004", "3.0", "2027-03-31"),
            {"xlsx"},
        ),
        (
            "kb/DOC-31_XC-HR-EQT-013_核心员工期权授予与行权指引.pdf",
            ("XC-HR-EQT-013", "25%", "Cliff", "90 天", "全部作废"),
            {"pdf"},
        ),
        (
            "kb/DOC-32_XC-FIN-TAX-022_年终奖与离职补偿金个税实务.pdf",
            ("XC-FIN-TAX-022", "单独计税", "综合所得", "3 倍", "免征个人所得税"),
            {"pdf"},
        ),
        (
            "kb/DOC-33_2026-12_差旅报销与年度关账待审台账.xlsx",
            (
                "TRV-2026-1201",
                "TRV-2026-1203",
                "TRV-2026-1206",
                "FIN-ADV-2026-1122",
                "2026-12-25 18:00",
            ),
            {"xlsx"},
        ),
        (
            "kb/DOC-34_XC-RD-ARCH-032_生产变更封网与高危发布规范.md",
            ("XC-RD-ARCH-032", "2026-12-24", "2027-01-03", "MOCK003", "DDL", "回滚脚本"),
            {"markdown"},
        ),
        (
            "kb/DOC-35_苏晚晴2026-12跨年年假与面谈备忘.md",
            ("HRBP-PRIVATE-NOTE-202612", "B-08", "XC1004", "XC1008", "2027-03-31"),
            {"markdown"},
        ),
        (
            "kb/DOC-36_陆知微2026-12封网值班与架构复盘笔记.md",
            (
                "LU-PERSONAL-ARCH-202612",
                "athena-core",
                "350ms",
                "SQL-ROLLBACK-20261226-01",
                "XC1001",
            ),
            {"markdown"},
        ),
        (
            "attachments/ATT-01_陈思远_录用通知书_OFFER-2026-1108.pdf",
            ("OFFER-2026-1108", "陈思远", "XC1001", "dept_mock_platform", "2026-12-01", "MOCK005"),
            {"pdf"},
        ),
        (
            "attachments/ATT-02_许知夏_试用期转正评估表_XC1002.docx",
            ("许知夏", "XC1002", "2026-11-30", "1.0", "MOCK003"),
            {"docx", "docx_table"},
        ),
        (
            "attachments/ATT-03_韩景明_离职交接与待清算清单_XC1003.xlsx",
            ("IT-MBP-2023-088", "XC-SN-88412", "FIN-ADV-2026-0914", "3.5", "2400"),
            {"xlsx"},
        ),
        (
            "attachments/ATT-04_郑语桐_跨部门调岗交接汇报_XC1004.pptx",
            ("郑语桐", "XC1004", "dept_mock_hr", "2026-12-01", "4.0"),
            {"pptx", "pptx_table"},
        ),
        (
            "attachments/ATT-12_星辰智联云算力采购框架合同_CTR-2026-1120.pdf",
            ("CTR-2026-1120", "云枢算力科技有限公司", "360,000.00", "LEG-REV-2026-0418"),
            {"pdf"},
        ),
    ],
)
def test_non_ocr_documents_parse_with_exact_keywords_and_locators(
    relpath: str,
    expected_keywords: tuple[str, ...],
    expected_locator_kinds: set[str],
) -> None:
    """无 OCR 模式下精准抽取文本、表格、工作表、幻灯片与 PDF 页码定位器。"""
    parser = LocalDocumentParser(enable_ocr=False)
    file_path = GENERATED_DIR / relpath
    parsed = parser.parse_sync(file_path.name, file_path.read_bytes())

    assert parsed.blocks, f"empty_blocks:{relpath}"
    assert "xlsx_formula_cache_missing" not in parsed.warnings
    full_text = "\n".join(block.text for block in parsed.blocks)
    for keyword in expected_keywords:
        assert keyword in full_text, f"keyword_missing:{keyword} in {relpath}"

    observed_kinds = {block.locator.kind for block in parsed.blocks}
    assert expected_locator_kinds <= observed_kinds, (
        f"locator_kinds_mismatch:{relpath} expected={expected_locator_kinds} got={observed_kinds}"
    )
    if file_path.suffix == ".pdf":
        pages = {block.locator.page for block in parsed.blocks}
        assert {1, 2} <= pages, f"pdf_pages_missing:{relpath} got={pages}"
    if file_path.suffix == ".xlsx":
        assert all(block.locator.sheet and block.locator.cell_range for block in parsed.blocks)
    if file_path.suffix == ".pptx":
        assert all(block.locator.slide and block.locator.shape for block in parsed.blocks)


@pytest.mark.parametrize(
    ("relpath", "expected_page_count"),
    [
        ("kb/DOC-03_XC-HR-2026-003_离职交接与清算管理制度.pdf", 2),
        ("kb/DOC-10_2026Q4_财务HR协同结算指南.pdf", 2),
        ("kb/DOC-15_XC-IT-2026-009_IT工单服务与权限SLA规范.pdf", 2),
        ("kb/DOC-19_新员工背调核验与录用风控指引.pdf", 2),
        ("kb/DOC-25_XC-SEC-2026-011_信息安全与数据分级规范.pdf", 3),
        ("kb/DOC-31_XC-HR-EQT-013_核心员工期权授予与行权指引.pdf", 3),
        ("kb/DOC-32_XC-FIN-TAX-022_年终奖与离职补偿金个税实务.pdf", 3),
        ("attachments/ATT-01_陈思远_录用通知书_OFFER-2026-1108.pdf", 2),
        ("attachments/ATT-12_星辰智联云算力采购框架合同_CTR-2026-1120.pdf", 2),
    ],
)
def test_native_chinese_pdfs_never_trigger_ocr_even_when_ocr_enabled(
    relpath: str,
    expected_page_count: int,
) -> None:
    """纯原生 Type0 中文 PDF（含 V3 三页 PDF）在 enable_ocr=True 时必须零触发 PaddleOCR。"""
    parser = LocalDocumentParser(enable_ocr=True, ocr_engine=_ForbiddenOcrEngine())
    file_path = GENERATED_DIR / relpath
    parsed = parser.parse_sync(file_path.name, file_path.read_bytes())
    assert len(parsed.blocks) == expected_page_count
    assert {block.locator.page for block in parsed.blocks} == set(range(1, expected_page_count + 1))


def test_scanned_no_text_pdf_att09_triggers_empty_text_degradation() -> None:
    """ATT-09 无文本层扫描件 PDF 须通过基础校验，但在无 OCR 路径稳定触发空文本错误码。"""
    att09_path = GENERATED_DIR / "attachments/ATT-09_扫描件无文本层_离职交接确认单_XC1003.pdf"
    parser = LocalDocumentParser(enable_ocr=False)
    parser.validate_file(att09_path.name, att09_path)

    with pytest.raises(ValueError, match="knowledge_document_text_empty"):
        parser.parse_sync(att09_path.name, att09_path.read_bytes())

    extractor = LocalSessionDocumentExtractor(parser=parser)
    result = asyncio.run(extractor.extract(att09_path.name, att09_path))
    assert result.status is SessionFileExtractionStatus.FAILED
    assert result.error_code == "document_no_extractable_text"


def test_att07_jpeg_contains_mock_exif_and_is_stripped_by_vision_normalizer() -> None:
    """ATT-07 拍照件原始包含无害模拟 EXIF/GPS，经视觉图片归一化后必须 100% 清除。"""
    att07_path = GENERATED_DIR / "attachments/ATT-07_韩景明_IT设备签收单拍照件_XC1003.jpg"
    raw_bytes = att07_path.read_bytes()

    with Image.open(io.BytesIO(raw_bytes)) as original:
        orig_exif = original.getexif()
        assert orig_exif.get(Base.Make) == "AthenaMockCam"
        assert orig_exif.get(Base.Model) == "MockPhone-16"
        assert orig_exif.get(Base.Software) == "AthenaTestBuilder 1.0"
        gps_ifd = orig_exif.get_ifd(0x8825)
        assert gps_ifd, "mock_gps_exif_must_exist_before_normalization"

    normalizer = LocalVisionImageNormalizer()
    normalized = asyncio.run(
        normalizer.normalize(
            file_id="file_att07_test",
            filename=att07_path.name,
            declared_mime_type="image/jpeg",
            raw_bytes=raw_bytes,
        )
    )
    assert normalized.mime_type == "image/jpeg"
    decoded_bytes = base64.b64decode(normalized.data_base64)
    with Image.open(io.BytesIO(decoded_bytes)) as cleaned:
        cleaned_exif = cleaned.getexif()
        assert len(cleaned_exif) == 0
        assert len(cleaned_exif.get_ifd(0x8825)) == 0
        assert "exif" not in cleaned.info


def test_v2_boundary_attachments_encrypted_pdf_macro_docx_contract_pdf_and_invoice_png() -> None:
    """用真实 LocalSessionDocumentExtractor（零 monkeypatch）验证 ATT-10/11/12/13 边界提取行为。"""
    extractor = LocalSessionDocumentExtractor()

    # 1. ATT-10：真实 PDF 1.4 /Encrypt 密码保护 PDF -> document_encrypted。
    att10 = GENERATED_DIR / "attachments/ATT-10_机密_2026薪酬绩效校准密件_加密样本.pdf"
    res10 = asyncio.run(extractor.extract(att10.name, att10))
    assert res10 == SessionDocumentExtractionResult.failed("document_encrypted")

    # 2. ATT-11：真实含 word/vbaProject.bin 宏 DOCX -> document_macro_unsupported。
    att11 = GENERATED_DIR / "attachments/ATT-11_含宏异常_2026年11月差旅报销汇总表.docx"
    res11 = asyncio.run(extractor.extract(att11.name, att11))
    assert res11 == SessionDocumentExtractionResult.failed("document_macro_unsupported")

    # 3. ATT-12：原生中文合同 PDF -> ready 且完整保留关键编号与金额。
    att12 = GENERATED_DIR / "attachments/ATT-12_星辰智联云算力采购框架合同_CTR-2026-1120.pdf"
    res12 = asyncio.run(extractor.extract(att12.name, att12))
    assert res12.status is SessionFileExtractionStatus.READY
    assert res12.text is not None
    for token in ("CTR-2026-1120", "云枢算力科技有限公司", "360,000.00", "LEG-REV-2026-0418"):
        assert token in res12.text

    # 4. ATT-13：高明轩上海张江酒店电子发票 PNG -> 视觉归一化通过且输出合法 PNG。
    att13 = GENERATED_DIR / "attachments/ATT-13_高明轩_上海张江酒店电子发票_TRV-2026-1102.png"
    normalizer = LocalVisionImageNormalizer()
    norm13 = asyncio.run(
        normalizer.normalize(
            file_id="file_att13_test",
            filename=att13.name,
            declared_mime_type="image/png",
            raw_bytes=att13.read_bytes(),
        )
    )
    assert norm13.mime_type == "image/png"
    assert norm13.width > 0 and norm13.height > 0


def test_v3_boundary_attachments_multipage_scanned_webp_cards_and_corrupt_png() -> None:
    """验证 V3 新增 6 份附件（ATT-14 至 ATT-19）真实提取、WebP 归一化与伪装损坏 PNG 拦截。"""
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    extractor = LocalSessionDocumentExtractor()
    normalizer = LocalVisionImageNormalizer()

    # 1. ATT-14：3 页纯图像扫描件无文本层 PDF -> 校验页数为 3 且返回 document_no_extractable_text。
    att14 = GENERATED_DIR / "attachments/ATT-14_多页扫描件无文本层_2026Q4绩效校准纪要_3页.pdf"
    raw_att14 = att14.read_bytes()
    assert b"BT\n" not in raw_att14
    pdf_doc = pdfium.PdfDocument(str(att14))
    try:
        assert len(pdf_doc) == 3
    finally:
        pdf_doc.close()
    res14 = asyncio.run(extractor.extract(att14.name, att14))
    assert res14 == SessionDocumentExtractionResult.failed("document_no_extractable_text")

    # 2. ATT-15：真实 WebP 登机牌行程图 -> 图像格式确为 WEBP 且通过视觉归一化器输出合法 image/webp。
    att15 = GENERATED_DIR / "attachments/ATT-15_成都分部出差登机牌与行程图_TRV-2026-1202.webp"
    att15_bytes = att15.read_bytes()
    with Image.open(io.BytesIO(att15_bytes)) as webp_img:
        assert webp_img.format == "WEBP"
    norm15 = asyncio.run(
        normalizer.normalize(
            file_id="file_att15_test",
            filename=att15.name,
            declared_mime_type="image/webp",
            raw_bytes=att15_bytes,
        )
    )
    assert norm15.mime_type == "image/webp"
    assert norm15.width > 0 and norm15.height > 0

    # 3. ATT-16 / ATT-17 / ATT-18：12 月差旅水单与封网审批 PNG 样张均通过视觉归一化。
    for att_rel in (
        "attachments/ATT-16_北京酒店住宿水单_TRV-2026-1203.png",
        "attachments/ATT-17_深圳酒店账单_TRV-2026-1206.png",
        "attachments/ATT-18_年终封网发布审批单截图.png",
    ):
        att_path = GENERATED_DIR / att_rel
        norm = asyncio.run(
            normalizer.normalize(
                file_id=f"file_{att_path.stem}",
                filename=att_path.name,
                declared_mime_type="image/png",
                raw_bytes=att_path.read_bytes(),
            )
        )
        assert norm.mime_type == "image/png"
        assert norm.width > 0 and norm.height > 0

    # 4. ATT-19：伪装 PNG 损坏图片负样本 -> LocalVisionImageNormalizer 抛出 vision_image_invalid。
    att19 = GENERATED_DIR / "attachments/ATT-19_伪装PNG损坏图片_负样本.png"
    with pytest.raises(VisionImageError) as err19:
        asyncio.run(
            normalizer.normalize(
                file_id="file_att19_test",
                filename=att19.name,
                declared_mime_type="image/png",
                raw_bytes=att19.read_bytes(),
            )
        )
    assert err19.value.code == "vision_image_invalid"
    with pytest.raises(ValueError, match="knowledge_image_invalid"):
        LocalDocumentParser(enable_ocr=False).validate_file(att19.name, att19)
