"""DOCX 交付、验收报告汇总与可选 PDF 导出（tasks 3.5/3.6）。

DOCX 是主格式；PDF 只是「把 DOCX 交给 LibreOffice 转」的便捷出口——不自研 PDF
排版引擎（design Non-Goal），转换结果依赖本机字体，因此 PDF 相关断言只检查
「中文是否可提取、是否嵌入了字体」，不绑定具体商业字体名。

验收报告随稿产出：要素校验 + 版式实测 + 字体决策 + 交付结果，一条不落，
每条都有 rule_id / status / locator / evidence（见 report.py）。
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..locator import document_locator, field_locator
from ..proofread import ProofreadResult, proofread
from . import elements, typeset
from .fonts import FontResolution, resolve_fonts
from .report import AcceptanceReport, CheckItem

# 版式容差：mm 级 0.1mm（够吸收 twips 取整误差），行距 0.01pt
LENGTH_TOLERANCE_MM = 0.1
LINE_SPACING_TOLERANCE_PT = 0.01

# 校对表里与 elements 重复的 P0 规则：交付路径只保留 elements 那份，
# 避免同一缺陷在验收报告里重复计数（两处共用 format_check 的判定逻辑）。
_ELEMENT_OWNED_P0_RULES: frozenset[str] = frozenset(
    {"pf.p0.missing_element", "pf.p0.doc_number_format", "pf.p0.date_format"}
)


class PdfExportError(RuntimeError):
    """PDF 导出失败（soffice 缺失或转换无产出），消息必须可直接照做。"""


@dataclass(slots=True)
class DeliveryResult:
    """一次交付的全部产物：DOCX 必有，PDF 可选，报告与字体决策随附。"""

    docx: bytes
    pdf: bytes | None
    report: AcceptanceReport
    fonts: FontResolution


# ---------------------------------------------------------------------------
# 版式与字体的验收项
# ---------------------------------------------------------------------------


def _within(actual: float, expected: float, tolerance: float) -> bool:
    return abs(actual - expected) <= tolerance


def _layout_items(facts: dict[str, Any]) -> list[CheckItem]:
    """把实测版式事实逐项对照 GB/T 9704-2012 第 6/10 章。"""
    items: list[CheckItem] = []
    locator = document_locator()

    page_w, page_h = facts["page_size_mm"]
    page_ok = _within(page_w, typeset.PAGE_W_MM, LENGTH_TOLERANCE_MM) and _within(
        page_h, typeset.PAGE_H_MM, LENGTH_TOLERANCE_MM
    )
    items.append(
        CheckItem(
            rule_id="gov.layout.page_size",
            status="pass" if page_ok else "fail",
            severity="P0",
            locator=locator,
            evidence=f"实测页面 {page_w}mm×{page_h}mm（A4 应为 210mm×297mm）",
            suggestion=None if page_ok else "页面尺寸必须为 A4（210mm×297mm）",
        )
    )

    margins = facts["margins_mm"]
    expected_margins = {
        "top": typeset.MARGIN_TOP_MM,
        "left": typeset.MARGIN_LEFT_MM,
        "bottom": typeset.MARGIN_BOTTOM_MM,
        "right": typeset.MARGIN_RIGHT_MM,
    }
    bad_margins = [
        f"{key}={margins[key]}mm(应 {expected}mm)"
        for key, expected in expected_margins.items()
        if not _within(margins[key], expected, LENGTH_TOLERANCE_MM)
    ]
    items.append(
        CheckItem(
            rule_id="gov.layout.margins",
            status="fail" if bad_margins else "pass",
            severity="P0",
            locator=locator,
            evidence=(
                f"实测天头{margins['top']}mm、订口{margins['left']}mm、"
                f"地脚{margins['bottom']}mm、翻口{margins['right']}mm"
                + (f"；偏差：{'；'.join(bad_margins)}" if bad_margins else "")
            ),
            suggestion=(
                "按 GB/T 9704-2012 设置天头 37mm、订口 28mm、地脚 35mm、翻口 26mm"
                if bad_margins
                else None
            ),
        )
    )

    area_w, area_h = facts["text_area_mm"]
    area_ok = _within(area_w, typeset.TEXT_AREA_MM[0], LENGTH_TOLERANCE_MM) and _within(
        area_h, typeset.TEXT_AREA_MM[1], LENGTH_TOLERANCE_MM
    )
    items.append(
        CheckItem(
            rule_id="gov.layout.text_area",
            status="pass" if area_ok else "fail",
            severity="P0",
            locator=locator,
            evidence=f"实测版心 {area_w}mm×{area_h}mm（应 156mm×225mm，容差 0.1mm）",
            suggestion=None if area_ok else "调整页边距使版心为 156mm×225mm",
        )
    )

    line_ok = _within(
        facts["line_spacing_pt"], 28.0, LINE_SPACING_TOLERANCE_PT
    ) and facts["line_spacing_rule"] == "exact"
    items.append(
        CheckItem(
            rule_id="gov.layout.line_spacing",
            status="pass" if line_ok else "fail",
            severity="P0",
            locator=locator,
            evidence=(
                f"实测固定行距 {facts['line_spacing_pt']}pt"
                f"（w:line={round(facts['line_spacing_pt'] * 20)} twips，"
                f"lineRule={facts['line_spacing_rule']}）"
            ),
            suggestion=None if line_ok else "正文行距设为固定值 28 磅（560 twips）",
        )
    )

    indent_ok = facts["first_line_indent_twips"] == typeset.FIRST_LINE_INDENT_TWIPS
    items.append(
        CheckItem(
            rule_id="gov.layout.first_line_indent",
            status="pass" if indent_ok else "fail",
            severity="P0",
            locator=locator,
            evidence=(
                f"实测首行缩进 {facts['first_line_indent_twips']} twips"
                f"（{facts['first_line_indent_pt']}pt = 2 字符 × 三号 16pt）"
            ),
            suggestion=None if indent_ok else "正文首行缩进设为 2 字符（640 twips）",
        )
    )

    page_number = facts.get("page_number", {})
    page_number_ok = (
        bool(page_number.get("odd"))
        and bool(page_number.get("even"))
        and page_number.get("odd_alignment") == "right"
        and page_number.get("even_alignment") == "left"
    )
    items.append(
        CheckItem(
            rule_id="gov.layout.page_number",
            status="pass" if page_number_ok else "fail",
            severity="P0",
            locator=field_locator("page_number"),
            evidence=(
                "奇偶页页脚均含 PAGE 域；单页码居右空一字、双页码居左空一字；"
                f"实测数字字号 {page_number.get('font_pt')}pt（GB 要求 4 号=14pt），两侧一字线"
            ),
            suggestion=(
                None
                if page_number_ok
                else "页码应为 4 号宋体「— N —」，单页居右、双页居左各空一字"
            ),
        )
    )
    return items


def _font_items(fonts: FontResolution) -> list[CheckItem]:
    """逐角色报告字体决策：用了首选即 pass，回退即 warn 并列出缺失族名。"""
    items: list[CheckItem] = []
    fallback_by_role = {entry["role"]: entry for entry in fonts.fallbacks}
    for role, chain in fonts.chains.items():
        chosen = fonts.family_for(role)
        requested = chain[0]
        degraded = role in fallback_by_role
        if not degraded:
            evidence = f"角色「{role}」使用首选字体「{requested}」"
            suggestion = None
        else:
            entry = fallback_by_role[role]
            missing = "、".join(entry["missing"])
            evidence = (
                f"角色「{role}」首选「{requested}」缺失，按声明顺序回退为「{chosen}」；"
                f"缺失族名：{missing}"
            )
            if chosen in fonts.missing:
                # 连兜底族名本机也没有：文档仍写该族名，由 Word/WPS 替代渲染
                evidence += f"；本机亦未检测到「{chosen}」，打开时由替代字体渲染"
            suggestion = (
                f"安装「{requested}」或调整字体回退链；严格模式下可直接失败而不降级"
            )
        items.append(
            CheckItem(
                rule_id=f"gov.font.{role}",
                status="warn" if degraded else "pass",
                severity="P1",
                locator=document_locator(),
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    return items


# ---------------------------------------------------------------------------
# 校对闸门（task 5.1）：P0 校对问题必须阻断交付
# ---------------------------------------------------------------------------


def _proofread_fields(doc: elements.GongwenDocument) -> dict[str, str | None]:
    """从公文要素里取校对需要的字段（与 proofread 的 fields 契约对齐）。"""
    return {
        "title": doc.title,
        "addressee": doc.addressee,
        "issuer_signature": doc.issuer_signature,
        "date": doc.date,
        "doc_number": doc.doc_number,
        "issuer_mark": doc.issuer_mark,
        "copy_number": doc.copy_number,
        "secrecy": doc.secrecy,
        "urgency": doc.urgency,
        "signer": doc.signer,
        "note": doc.note,
        "printing_org": doc.printing_org,
        "printing_date": doc.printing_date,
    }


def _proofread_items(doc: elements.GongwenDocument) -> list[CheckItem]:
    """执行规则校对，把问题逐条并入验收报告。

    校对只报告不改写（spec「只报告不改写」），因此这里只把 issue 映射成
    CheckItem：P0 失败 → status=fail/severity=P0（阻断交付），P1/P2 失败 →
    status=warn（进报告但不阻断）。

    要素类 P0 规则（缺要素 / 发文字号 / 成文日期）在 elements 与校对表里各有一份
    ——两者共用 format_check 的判定逻辑。交付路径只保留 elements 那份，避免同
    一缺陷在验收报告里出现两次、把 P0 计数翻倍。校对单独调用时仍全量覆盖。
    """
    result: ProofreadResult = proofread(doc.body, fields=_proofread_fields(doc))
    items: list[CheckItem] = []
    for issue in result.issues:
        if issue.severity == "P0" and issue.rule_id in _ELEMENT_OWNED_P0_RULES:
            continue
        blocking = issue.severity == "P0"
        items.append(
            CheckItem(
                rule_id=f"gov.proofread.{issue.rule_id}",
                status="fail" if blocking else "warn",
                severity=issue.severity,
                locator=issue.locator,
                evidence=f"{issue.message}（命中：{issue.evidence}）",
                suggestion=issue.suggestion or None,
            )
        )
    return items


# ---------------------------------------------------------------------------
# 渲染入口
# ---------------------------------------------------------------------------


def render(
    doc: elements.GongwenDocument,
    *,
    want_pdf: bool = False,
    fonts: FontResolution | None = None,
    strict: bool | None = None,
    skip_proofread: bool = False,
) -> DeliveryResult:
    """渲染 DOCX（可选 PDF）并产出验收报告。

    `fonts` 显式给出时直接使用（此时 `strict` 不再生效）；否则按本机字体解析，
    `strict=None` 读环境变量 FD_SHILUN_FONT_STRICT。

    字体严格模式缺字体时不返回半成品：直接抛 MissingFontError 让调用方失败，
    避免「看着成功、实际走样」（spec 严格模式失败）。

    `skip_proofread=True` 仅供「只做版式验收」的内部路径使用；成稿交付
    MUST NOT 跳过校对闸门（spec「公文成稿交付」：P0 问题不得标记为可交付）。
    """
    resolution = fonts if fonts is not None else resolve_fonts(strict=strict)

    document = typeset.build_document(doc, resolution)
    buffer = io.BytesIO()
    document.save(buffer)
    docx_bytes = buffer.getvalue()

    facts = typeset.layout_facts(io.BytesIO(docx_bytes))

    items: list[CheckItem] = []
    items.extend(elements.required_elements(doc))
    items.extend(elements.validate_elements(doc))
    items.extend(_layout_items(facts))
    items.extend(_font_items(resolution))
    if not skip_proofread:
        items.extend(_proofread_items(doc))
    items.append(
        CheckItem(
            rule_id="gov.deliver.docx",
            status="pass",
            severity=None,
            locator=document_locator(),
            evidence=(
                f"DOCX 已生成：{len(docx_bytes)} 字节，"
                f"{len(document.paragraphs)} 段，{len(document.sections)} 节"
            ),
        )
    )

    pdf_bytes: bytes | None = None
    if want_pdf:
        pdf_bytes = export_pdf(docx_bytes)
        items.append(
            CheckItem(
                rule_id="gov.deliver.pdf",
                status="pass",
                severity=None,
                locator=document_locator(),
                evidence=(
                    f"PDF 已由 LibreOffice 从 DOCX 转换：{len(pdf_bytes)} 字节；"
                    "中文字形由转换器嵌入"
                ),
            )
        )

    report = AcceptanceReport(doc_kind="gongwen", items=items)
    return DeliveryResult(
        docx=docx_bytes, pdf=pdf_bytes, report=report, fonts=resolution
    )


# ---------------------------------------------------------------------------
# PDF：只做格式转换，不做排版
# ---------------------------------------------------------------------------


def _find_soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def export_pdf(docx_bytes: bytes, *, timeout: int = 120) -> bytes:
    """用 LibreOffice 无头模式把 DOCX 转成 PDF。

    独立 user profile（-env:UserInstallation）是必要的：若桌面已有一个
    LibreOffice 实例在跑，不带该参数的 headless 调用会被它接管并直接退出，
    表现为「命令成功但没有输出文件」。
    """
    binary = _find_soffice()
    if not binary:
        raise PdfExportError(
            "未找到 LibreOffice（soffice）：PDF 导出需要先安装 LibreOffice 并确保 "
            "soffice 在 PATH 中；或改用 DOCX 交付（DOCX 为主格式，PDF 为可选项）"
        )

    with tempfile.TemporaryDirectory(prefix="fd-gongwen-pdf-") as tmp:
        tmpdir = Path(tmp)
        source = tmpdir / "gongwen.docx"
        source.write_bytes(docx_bytes)
        profile = tmpdir / "lo-profile"
        command = [
            binary,
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(tmpdir),
            str(source),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PdfExportError(
                f"LibreOffice 转换超时（>{timeout}s）：可缩短文档或调大 timeout"
            ) from exc
        except OSError as exc:
            raise PdfExportError(f"无法执行 LibreOffice（{binary}）：{exc}") from exc

        target = tmpdir / "gongwen.pdf"
        if not target.is_file() or target.stat().st_size == 0:
            stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
            stdout = (completed.stdout or b"").decode("utf-8", "replace").strip()
            detail = (stderr or stdout)[:800] or "无输出"
            raise PdfExportError(
                f"LibreOffice 未产出 PDF（返回码 {completed.returncode}）：{detail}；"
                "请确认 LibreOffice 安装完整且可写临时目录"
            )
        return target.read_bytes()


def pdf_text_and_fonts(pdf_bytes: bytes) -> tuple[str, list[str]]:
    """提取 PDF 文本与嵌入字体基名（pypdf 可选依赖；缺失时返回空结果）。"""
    try:
        from pypdf import PdfReader
    except Exception:  # pragma: no cover - 取决于部署环境
        return "", []

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return "", []

    texts: list[str] = []
    fonts: list[str] = []

    def collect(resources: Any) -> None:
        if resources is None:
            return
        try:
            resources = resources.get_object()
        except Exception:
            pass
        font_dict = resources.get("/Font") if hasattr(resources, "get") else None
        if not font_dict:
            return
        try:
            font_dict = font_dict.get_object()
        except Exception:
            pass
        if not hasattr(font_dict, "items"):
            return
        for _key, value in font_dict.items():
            try:
                font = value.get_object()
            except Exception:
                continue
            base = font.get("/BaseFont") if hasattr(font, "get") else None
            if not base:
                continue
            name = str(base).lstrip("/")
            # 子集前缀形如 ABCDEF+SimSun，去掉后便于阅读与去重
            if "+" in name:
                name = name.split("+", 1)[1]
            if name not in fonts:
                fonts.append(name)

    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            texts.append("")
        try:
            collect(page.get("/Resources"))
        except Exception:
            continue

    return "\n".join(texts), fonts


__all__ = [
    "DeliveryResult",
    "PdfExportError",
    "render",
    "export_pdf",
    "pdf_text_and_fonts",
]
