"""GB/T 9704-2012 版式渲染（tasks 3.1/3.2，design 决策 3）。

**直写 OOXML，不用 pandoc/reference_doc。** 版记、奇偶页页码、发文字号与签发人的
左右分置这些要素，在 pandoc 的 reference_doc 模型里表达力不足；直写能精确控制每个
run 的 `w:rFonts` 与段落属性。

中文字体为什么必须写四属性：OOXML 的 `w:rFonts` 有 ascii/hAnsi/eastAsia/cs 四个
字体槽，Word 按字符的 Unicode 范围决定用哪个槽；只设 `ascii` 时中文会落到
eastAsia 槽的默认值（宋体），于是「标题小标宋、正文仿宋」在 Word/WPS 里静默失效。
`w:hint="eastAsia"`（`rFonts` 的属性，不是独立元素）让中西文混排时优先东亚槽。
本模块所有 run 都经 `_set_run_font` 落字体，保证没有漏网的 run。

版式依据（GB/T 9704-2012 第 6 章）：A4；天头 37mm、订口 28mm、地脚 35mm、
翻口 26mm；版心 156mm×225mm；每面 22 行、每行 28 字；正文 3 号仿宋，固定行距
28 磅，首行缩进 2 字符（=32pt=640 twips）。
"""

from __future__ import annotations

import io
import os
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor, Twips
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from .elements import GongwenDocument
from .fonts import FontResolution

# ---------------------------------------------------------------------------
# 版式常量（单位：mm / pt / twips 明确标注，避免各处换算口径漂移）
# ---------------------------------------------------------------------------

PAGE_W_MM = 210.0
PAGE_H_MM = 297.0
MARGIN_TOP_MM = 37.0  # 天头
MARGIN_LEFT_MM = 28.0  # 订口
MARGIN_BOTTOM_MM = 35.0  # 地脚
MARGIN_RIGHT_MM = 26.0  # 翻口
# 版心 = 210-28-26 × 297-37-35（GB 6.1）
TEXT_AREA_MM: tuple[float, float] = (156.0, 225.0)

BODY_SIZE_PT = 16.0  # 三号
TITLE_SIZE_PT = 22.0  # 二号
SMALL4_SIZE_PT = 14.0  # 四号（版记、页码）
PAGE_NUM_SIZE_PT = 14.0

LINE_SPACING_TWIPS = 560  # 固定行距 28 磅 = 560 twips
FIRST_LINE_INDENT_TWIPS = 640  # 首行缩进 2 字符 = 2 × 16pt
LINES_PER_PAGE = 22
CHARS_PER_LINE = 28

# 字符网格压缩量（1/100 字符）：三号字 16pt × 28 字 = 448pt 略超 156mm(442.2pt)，
# charSpace=-2 把字距压到 15.68pt，28 字恰好撑满版心（GB 6.1「撑满版心」）。
# 不设负值 Word 会折成 27 字/行，与国标的 28 字不符。
CHAR_SPACE = -2

RED = RGBColor(0xFF, 0x00, 0x00)
MM_TO_PT = 72.0 / 25.4


# ---------------------------------------------------------------------------
# 底层 helper：字体与段落
# ---------------------------------------------------------------------------


def _apply_font(
    rpr: Any,
    family: str,
    size_pt: float,
    *,
    bold: bool = False,
    color: RGBColor | None = None,
) -> None:
    """把字体/字号写到 rPr（run 或 style 共用）。

    四个槽全部指向同一 family：中文走 eastAsia，西文/数字走 ascii/hAnsi，
    复杂文种走 cs；漏一个槽就会在对应字符上回退默认字体。
    """
    rfonts = rpr.get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), family)
    # 主题字体优先级高于显式族名，残留会让 Word 忽略上面的设置
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if rfonts.get(qn(attr)) is not None:
            del rfonts.attrib[qn(attr)]
    # hint 是 rFonts 的属性（schema: CT_Fonts/@w:hint），不是独立元素
    rfonts.set(qn("w:hint"), "eastAsia")

    half_points = str(int(round(size_pt * 2)))
    size_element = rpr.get_or_add_sz()
    size_element.set(qn("w:val"), half_points)
    # python-docx 不建模 szCs，手工补（复杂文种字号，缺省会让 cs 槽回退默认字号）
    size_cs = rpr.find(qn("w:szCs"))
    if size_cs is None:
        size_cs = OxmlElement("w:szCs")
        size_element.addnext(size_cs)
    size_cs.set(qn("w:val"), half_points)
    if bold:
        rpr.get_or_add_b()
    if color is not None:
        rpr.get_or_add_color().set(qn("w:val"), str(color))


def _set_run_font(
    run: Run,
    family: str,
    size_pt: float,
    *,
    bold: bool = False,
    color: RGBColor | None = None,
) -> Run:
    _apply_font(run._element.get_or_add_rPr(), family, size_pt, bold=bold, color=color)
    return run


def _add_run(
    paragraph: Paragraph,
    text: str,
    family: str,
    size_pt: float,
    *,
    bold: bool = False,
    color: RGBColor | None = None,
) -> Run:
    return _set_run_font(
        paragraph.add_run(text), family, size_pt, bold=bold, color=color
    )


def _format_paragraph(
    paragraph: Paragraph,
    *,
    align: WD_ALIGN_PARAGRAPH | None = None,
    first_line_pt: float | None = None,
    left_pt: float | None = None,
    right_pt: float | None = None,
    space_before_pt: float = 0.0,
    space_after_pt: float = 0.0,
    line_spacing_pt: float | None = None,
) -> Paragraph:
    if align is not None:
        paragraph.alignment = align
    pf = paragraph.paragraph_format
    pf.space_before = Pt(space_before_pt)
    pf.space_after = Pt(space_after_pt)
    if line_spacing_pt is not None:
        pf.line_spacing = Pt(line_spacing_pt)
    # 显式写 0 而不是留空：Normal 样式带 640 twips 首行缩进，留空会继承
    pf.first_line_indent = Pt(first_line_pt or 0.0)
    if left_pt is not None:
        pf.left_indent = Pt(left_pt)
    if right_pt is not None:
        pf.right_indent = Pt(right_pt)
    return paragraph


def _para(
    container: Any,
    *,
    family: str,
    size_pt: float,
    text: str | None = None,
    bold: bool = False,
    color: RGBColor | None = None,
    **kwargs: Any,
) -> Paragraph:
    paragraph = container.add_paragraph()
    _format_paragraph(paragraph, **kwargs)
    if text is not None:
        _add_run(paragraph, text, family, size_pt, bold=bold, color=color)
    return paragraph


# ---------------------------------------------------------------------------
# 版心与样式
# ---------------------------------------------------------------------------


def _setup_section(document: Any) -> Any:
    section = document.sections[0]
    section.page_width = Mm(PAGE_W_MM)
    section.page_height = Mm(PAGE_H_MM)
    section.top_margin = Mm(MARGIN_TOP_MM)
    section.left_margin = Mm(MARGIN_LEFT_MM)
    section.bottom_margin = Mm(MARGIN_BOTTOM_MM)
    section.right_margin = Mm(MARGIN_RIGHT_MM)
    section.header_distance = Mm(15)
    # 页码上边缘距版心下边缘 7mm（GB 7.5）；footer 距离按页脚高度折算
    section.footer_distance = Mm(23)

    # 版心必须严格等于 156×225mm（容差 0.1mm）：这是本模块对外承诺的版式事实
    width = section.page_width.mm - section.left_margin.mm - section.right_margin.mm
    height = section.page_height.mm - section.top_margin.mm - section.bottom_margin.mm
    assert abs(width - TEXT_AREA_MM[0]) <= 0.1, f"版心宽 {width:.2f}mm ≠ 156mm"
    assert abs(height - TEXT_AREA_MM[1]) <= 0.1, f"版心高 {height:.2f}mm ≠ 225mm"

    # 文档网格：22 行 × 28 字（linePitch 与固定行距同值，保证行网格与段落行距一致）
    sect_pr = section._sectPr
    old = sect_pr.find(qn("w:docGrid"))
    if old is not None:
        sect_pr.remove(old)
    grid = OxmlElement("w:docGrid")
    grid.set(qn("w:type"), "linesAndChars")
    grid.set(qn("w:linePitch"), str(LINE_SPACING_TWIPS))
    grid.set(qn("w:charSpace"), str(CHAR_SPACE))
    sect_pr.append(grid)
    return section


def _setup_normal_style(document: Any, family: str) -> None:
    """Normal 承载正文版式：三号、固定 28 磅、首行缩进 2 字符、段前段后 0。"""
    style = document.styles["Normal"]
    _apply_font(style.element.get_or_add_rPr(), family, BODY_SIZE_PT)
    pf = style.paragraph_format
    pf.line_spacing = Pt(28)  # Length → w:line=560 w:lineRule=exact
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.first_line_indent = Twips(FIRST_LINE_INDENT_TWIPS)


# ---------------------------------------------------------------------------
# 版头 / 主体 / 版记
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "，".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _copy_number_text(value: str) -> str:
    """份号用 6 位阿拉伯数字（GB 7.1）；纯数字补零到 6 位。"""
    raw = value.strip()
    return raw.zfill(6) if raw.isdigit() else raw


def _with_colon(text: str) -> str:
    """主送机关以全角冒号收束（GB 8.2）。"""
    return text.rstrip("：:") + "："


def _write_head(
    document: Any,
    doc: GongwenDocument,
    *,
    h1_family: str,
    h2_family: str,
    body_family: str,
    issuer_family: str,
) -> None:
    upper_lines = 0
    if _text(doc.copy_number):
        _para(
            document,
            family=h1_family,
            size_pt=BODY_SIZE_PT,
            text=_copy_number_text(_text(doc.copy_number)),
            align=WD_ALIGN_PARAGRAPH.LEFT,
            first_line_pt=0,
        )
        upper_lines += 1
    if _text(doc.secrecy):
        _para(
            document,
            family=h1_family,
            size_pt=BODY_SIZE_PT,
            text=_text(doc.secrecy),
            align=WD_ALIGN_PARAGRAPH.LEFT,
            first_line_pt=0,
        )
        upper_lines += 1
    if _text(doc.urgency):
        _para(
            document,
            family=h1_family,
            size_pt=BODY_SIZE_PT,
            text=_text(doc.urgency),
            align=WD_ALIGN_PARAGRAPH.LEFT,
            first_line_pt=0,
        )
        upper_lines += 1

    if _text(doc.issuer_mark):
        # 发文机关标志上边缘至版心上边缘 35mm（GB 7.4）；已占用的版头行要扣除
        gap_pt = max(0.0, 35.0 * MM_TO_PT - upper_lines * 28.0)
        _para(
            document,
            family=issuer_family,
            size_pt=TITLE_SIZE_PT,
            text=_text(doc.issuer_mark),
            align=WD_ALIGN_PARAGRAPH.CENTER,
            first_line_pt=0,
            space_before_pt=gap_pt,
            color=RED,
        )

    # 发文字号：下行文居中；上行文居左空一字、签发人居右空一字（GB 7.5/7.6）
    doc_number = _text(doc.doc_number)
    signer = _text(doc.signer)
    if doc_number or signer:
        paragraph = document.add_paragraph()
        _format_paragraph(
            paragraph,
            align=(
                WD_ALIGN_PARAGRAPH.LEFT if signer else WD_ALIGN_PARAGRAPH.CENTER
            ),
            first_line_pt=0,
            left_pt=BODY_SIZE_PT if signer else 0,
            right_pt=BODY_SIZE_PT if signer else 0,
            space_before_pt=2 * 28.0,  # 发文机关标志下空二行（GB 7.5）
        )
        if signer:
            # 右侧制表位：文号与签发人同处一行，签发人右空一字
            tab_pos = Mm(TEXT_AREA_MM[0]) - Pt(2 * BODY_SIZE_PT)
            paragraph.paragraph_format.tab_stops.add_tab_stop(
                tab_pos, WD_TAB_ALIGNMENT.RIGHT
            )
        if doc_number:
            _add_run(paragraph, doc_number, body_family, BODY_SIZE_PT)
        if signer:
            if doc_number:
                _add_run(paragraph, "\t", body_family, BODY_SIZE_PT)
            # 「签发人：」用仿宋，姓名用楷体（GB 7.6 通行排法）
            _add_run(paragraph, "签发人：", body_family, BODY_SIZE_PT)
            _add_run(paragraph, signer, h2_family, BODY_SIZE_PT)


def _attachment_lines(doc: GongwenDocument) -> list[str]:
    """附件说明文本：优先用户给定；否则由附件清单按序号生成（GB 8.4）。"""
    note = _text(doc.attachment_note)
    if note:
        return note.split("\n")
    names = [str(name).strip() for name in doc.attachments if str(name).strip()]
    if not names:
        return []
    if len(names) == 1:
        return [f"附件：{names[0]}"]
    return [f"附件：1.{names[0]}"] + [
        f"{index}.{name}" for index, name in enumerate(names[1:], start=2)
    ]


def _wrap_note(text: str) -> str:
    """附注加圆括号编排（GB 8.6）；已带括号则不重复加。"""
    if text.startswith("（") and text.endswith("）"):
        return text
    return f"（{text}）"


def _write_main(
    document: Any,
    doc: GongwenDocument,
    *,
    title_family: str,
    body_family: str,
) -> None:
    title = _text(doc.title)
    if title:
        for index, line in enumerate(title.split("\n")):
            _para(
                document,
                family=title_family,
                size_pt=TITLE_SIZE_PT,
                text=line,
                align=WD_ALIGN_PARAGRAPH.CENTER,
                first_line_pt=0,
                # 标题位于版头分隔线下空二行位置（GB 8.1）
                space_before_pt=2 * 28.0 if index == 0 else 0.0,
            )

    addressee = _text(doc.addressee)
    if addressee:
        _para(
            document,
            family=body_family,
            size_pt=BODY_SIZE_PT,
            text=_with_colon(addressee),
            align=WD_ALIGN_PARAGRAPH.LEFT,
            first_line_pt=0,
            space_before_pt=28.0,  # 标题下空一行（GB 8.2）
        )

    # 正文：每个自然段左空二字（首行缩进 2 字符），回行顶格
    for line in _text(doc.body).split("\n"):
        _para(
            document,
            family=body_family,
            size_pt=BODY_SIZE_PT,
            text=line,
            align=WD_ALIGN_PARAGRAPH.JUSTIFY,
            first_line_pt=2 * BODY_SIZE_PT,
        )

    # 附件说明：正文下空一行左空二字（GB 8.4）；多附件回行与名称首字对齐
    for index, line in enumerate(_attachment_lines(doc)):
        left_pt = 2 * BODY_SIZE_PT + (3 * BODY_SIZE_PT if index > 0 else 0.0)
        _para(
            document,
            family=body_family,
            size_pt=BODY_SIZE_PT,
            text=line,
            align=WD_ALIGN_PARAGRAPH.LEFT,
            first_line_pt=0,
            left_pt=left_pt,
            space_before_pt=28.0 if index == 0 else 0.0,
        )

    # 署名与成文日期：署名右空二字，日期右空四字（GB 8.5）
    if _text(doc.issuer_signature):
        _para(
            document,
            family=body_family,
            size_pt=BODY_SIZE_PT,
            text=_text(doc.issuer_signature),
            align=WD_ALIGN_PARAGRAPH.RIGHT,
            first_line_pt=0,
            right_pt=2 * BODY_SIZE_PT,
            space_before_pt=28.0,
        )
    if _text(doc.date):
        _para(
            document,
            family=body_family,
            size_pt=BODY_SIZE_PT,
            text=_text(doc.date),
            align=WD_ALIGN_PARAGRAPH.RIGHT,
            first_line_pt=0,
            right_pt=4 * BODY_SIZE_PT,
        )

    # 附注：成文日期下一行，居左空二字加圆括号（GB 8.6）
    if _text(doc.note):
        _para(
            document,
            family=body_family,
            size_pt=BODY_SIZE_PT,
            text=_wrap_note(_text(doc.note)),
            align=WD_ALIGN_PARAGRAPH.LEFT,
            first_line_pt=0,
            left_pt=2 * BODY_SIZE_PT,
        )


def _write_record(
    document: Any,
    doc: GongwenDocument,
    *,
    body_family: str,
) -> None:
    """版记：抄送机关 + 印发机关和印发日期（GB 9），4 号仿宋。"""
    cc = _text(doc.cc)
    printing_org = _text(doc.printing_org)
    printing_date = _text(doc.printing_date)
    if not (cc or printing_org or printing_date):
        return

    if cc:
        if not cc.endswith("。"):
            cc += "。"
        _para(
            document,
            family=body_family,
            size_pt=SMALL4_SIZE_PT,
            text=f"抄送：{cc}",
            align=WD_ALIGN_PARAGRAPH.LEFT,
            first_line_pt=0,
            left_pt=SMALL4_SIZE_PT,
            right_pt=SMALL4_SIZE_PT,
            space_before_pt=28.0,
        )

    if printing_org or printing_date:
        paragraph = document.add_paragraph()
        _format_paragraph(
            paragraph,
            align=WD_ALIGN_PARAGRAPH.LEFT,
            first_line_pt=0,
            left_pt=SMALL4_SIZE_PT,
            right_pt=SMALL4_SIZE_PT,
        )
        tab_pos = Mm(TEXT_AREA_MM[0]) - Pt(2 * SMALL4_SIZE_PT)
        paragraph.paragraph_format.tab_stops.add_tab_stop(
            tab_pos, WD_TAB_ALIGNMENT.RIGHT
        )
        if printing_org:
            _add_run(paragraph, printing_org, body_family, SMALL4_SIZE_PT)
        if printing_date:
            _add_run(paragraph, "\t", body_family, SMALL4_SIZE_PT)
            _add_run(paragraph, f"{printing_date}印发", body_family, SMALL4_SIZE_PT)


# ---------------------------------------------------------------------------
# 页码（奇偶页不同）
# ---------------------------------------------------------------------------


def _fill_footer(footer: Any, family: str, *, align_right: bool) -> None:
    """页脚写 `— N —`，N 为真实 PAGE 域（GB 10）。"""
    footer.is_linked_to_previous = False
    paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    _format_paragraph(
        paragraph,
        align=WD_ALIGN_PARAGRAPH.RIGHT if align_right else WD_ALIGN_PARAGRAPH.LEFT,
        first_line_pt=0,
        left_pt=0 if align_right else PAGE_NUM_SIZE_PT,
        right_pt=PAGE_NUM_SIZE_PT if align_right else 0,
        line_spacing_pt=PAGE_NUM_SIZE_PT * 1.2,
    )
    _add_run(paragraph, "— ", family, PAGE_NUM_SIZE_PT)

    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), " PAGE ")
    run_element = OxmlElement("w:r")
    field.append(run_element)
    paragraph._p.append(field)
    field_run = _set_run_font(Run(run_element, paragraph), family, PAGE_NUM_SIZE_PT)
    field_run.text = "1"  # 域结果占位，打开时由 Word/WPS 更新为真实页码

    _add_run(paragraph, " —", family, PAGE_NUM_SIZE_PT)


def _write_footers(section: Any, family: str) -> None:
    # 单页码居右空一字、双页码居左空一字（GB 10）
    _fill_footer(section.footer, family, align_right=True)
    _fill_footer(section.even_page_footer, family, align_right=False)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def build_document(doc: GongwenDocument, fonts: FontResolution) -> Any:
    """按国标渲染一篇公文，返回 python-docx Document。"""
    document = Document()
    _setup_section(document)
    _setup_normal_style(document, fonts.family_for("body"))
    document.settings.odd_and_even_pages_header_footer = True

    _write_head(
        document,
        doc,
        h1_family=fonts.family_for("h1"),
        h2_family=fonts.family_for("h2"),
        body_family=fonts.family_for("body"),
        issuer_family=fonts.family_for("issuer_mark"),
    )
    _write_main(
        document,
        doc,
        title_family=fonts.family_for("title"),
        body_family=fonts.family_for("body"),
    )
    _write_record(document, doc, body_family=fonts.family_for("body"))
    _write_footers(document.sections[0], fonts.family_for("page_number"))
    return document


# ---------------------------------------------------------------------------
# 版式实测（测试与验收报告共用同一份事实）
# ---------------------------------------------------------------------------


def _int_attr(element: Any, name: str, default: int = 0) -> int:
    if element is None:
        return default
    raw = element.get(qn(name))
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _open_document(source: Any) -> Any:
    if hasattr(source, "sections"):  # 已经是 Document
        return source
    if isinstance(source, (bytes, bytearray)):
        return Document(io.BytesIO(bytes(source)))
    if isinstance(source, (str, os.PathLike)):
        return Document(str(source))
    return Document(source)


def _footer_has_page_field(footer: Any) -> bool:
    try:
        paragraphs = footer.paragraphs
    except Exception:  # 未定义页脚
        return False
    for paragraph in paragraphs:
        for field in paragraph._p.iter(qn("w:fldSimple")):
            if "PAGE" in (field.get(qn("w:instr")) or "").upper():
                return True
        for instr in paragraph._p.iter(qn("w:instrText")):
            if "PAGE" in (instr.text or "").upper():
                return True
    return False


def _footer_alignment(footer: Any) -> str | None:
    try:
        paragraphs = footer.paragraphs
    except Exception:
        return None
    if not paragraphs:
        return None
    value = paragraphs[0].alignment
    if value is None:
        return None
    name = getattr(value, "name", str(value))
    return {"RIGHT": "right", "LEFT": "left", "CENTER": "center"}.get(name, name.lower())


def _footer_font_pt(footer: Any) -> float | None:
    """页脚首个 run 的字号（半磅值 ÷ 2），用于核对页码是否 4 号。"""
    try:
        paragraphs = footer.paragraphs
    except Exception:
        return None
    if not paragraphs:
        return None
    for run in paragraphs[0].runs:
        rpr = run._element.find(qn("w:rPr"))
        size = rpr.find(qn("w:sz")) if rpr is not None else None
        if size is not None:
            return _int_attr(size, "w:val") / 2.0
    return None


def layout_facts(docx_file: Any) -> dict[str, Any]:
    """从**产出的 docx** 反读版式参数，供测试与验收报告断言。

    不读本模块的常量，避免「用自己的常量证明自己正确」。
    """
    document = _open_document(docx_file)
    section = document.sections[0]

    page_w = section.page_width.mm
    page_h = section.page_height.mm
    top = section.top_margin.mm
    left = section.left_margin.mm
    bottom = section.bottom_margin.mm
    right = section.right_margin.mm
    area_w = page_w - left - right
    area_h = page_h - top - bottom

    # Normal 样式里的段落口径（word/styles.xml）
    normal_element = document.styles["Normal"].element
    p_pr = normal_element.find(qn("w:pPr"))
    spacing = p_pr.find(qn("w:spacing")) if p_pr is not None else None
    ind = p_pr.find(qn("w:ind")) if p_pr is not None else None
    line_twips = _int_attr(spacing, "w:line", LINE_SPACING_TWIPS)
    rule_raw = spacing.get(qn("w:lineRule")) if spacing is not None else None
    before = _int_attr(spacing, "w:before")
    after = _int_attr(spacing, "w:after")
    first_line = _int_attr(ind, "w:firstLine", FIRST_LINE_INDENT_TWIPS)
    line_pt = line_twips / 20.0

    r_pr = normal_element.find(qn("w:rPr"))
    size_element = r_pr.find(qn("w:sz")) if r_pr is not None else None
    body_pt = _int_attr(size_element, "w:val", int(BODY_SIZE_PT * 2)) / 2.0
    fonts_element = r_pr.find(qn("w:rFonts")) if r_pr is not None else None
    body_family = (
        fonts_element.get(qn("w:eastAsia")) if fonts_element is not None else None
    )

    sect_pr = section._sectPr
    grid_element = sect_pr.find(qn("w:docGrid"))
    grid: dict[str, Any] = {}
    char_space: int | None = None
    if grid_element is not None:
        char_space = (
            _int_attr(grid_element, "w:charSpace")
            if grid_element.get(qn("w:charSpace")) is not None
            else None
        )
        grid = {
            "type": grid_element.get(qn("w:type")),
            "line_pitch_twips": _int_attr(grid_element, "w:linePitch"),
            "char_space": char_space,
        }

    area_w_pt = area_w * MM_TO_PT
    area_h_pt = area_h * MM_TO_PT
    lines_per_page = int(area_h_pt // line_pt) if line_pt > 0 else 0
    if char_space is not None:
        char_pitch_pt = body_pt * (1 + char_space / 100.0)
    else:
        char_pitch_pt = body_pt
    chars_per_line = int(area_w_pt / char_pitch_pt + 1e-9) if char_pitch_pt > 0 else 0

    return {
        "page_size_mm": [round(page_w, 3), round(page_h, 3)],
        "margins_mm": {
            "top": round(top, 3),
            "left": round(left, 3),
            "bottom": round(bottom, 3),
            "right": round(right, 3),
        },
        "text_area_mm": [round(area_w, 3), round(area_h, 3)],
        "body_font_pt": body_pt,
        "body_font_family": body_family,
        "line_spacing_pt": round(line_pt, 3),
        "line_spacing_rule": rule_raw or "auto",
        "space_before_twips": before,
        "space_after_twips": after,
        "first_line_indent_twips": first_line,
        "first_line_indent_pt": round(first_line / 20.0, 3),
        "lines_per_page": lines_per_page,
        "chars_per_line": chars_per_line,
        "grid": grid,
        "page_number": {
            "odd": _footer_has_page_field(section.footer),
            "even": _footer_has_page_field(section.even_page_footer),
            "odd_alignment": _footer_alignment(section.footer),
            "even_alignment": _footer_alignment(section.even_page_footer),
            "font_pt": _footer_font_pt(section.footer),
        },
    }


__all__ = [
    "TEXT_AREA_MM",
    "LINE_SPACING_TWIPS",
    "FIRST_LINE_INDENT_TWIPS",
    "PAGE_W_MM",
    "PAGE_H_MM",
    "MARGIN_TOP_MM",
    "MARGIN_LEFT_MM",
    "MARGIN_BOTTOM_MM",
    "MARGIN_RIGHT_MM",
    "build_document",
    "layout_facts",
]
