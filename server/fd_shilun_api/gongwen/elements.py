"""公文要素校验（task 3.3）：完整性 + 发文字号/成文日期 + 印章/附件一致性。

要素清单取自 GB/T 9704-2012 7~10 章，共 18 项（见 `gb_checklist`）。哪些「必须
有」取决于文种、密级与行文方向，不能一刀切：

- 硬性必需（缺了就不是完整公文）：标题、主送机关、正文、发文机关署名、成文日期；
- 条件性要素（按文种/密级/行文方向标注）：份号仅涉密公文、签发人仅上行文、
  印发机关与日期仅需要版记的公文……缺了只记 P2 告警。
  若把条件性要素一律判 P0，一份合规的「不涉密通知」会因未标份号而不可交付，
  闸门随即失去意义（误报会让用户整体忽略报告）。

发文字号与成文日期的格式判定**不在这里重写**：统一走 format_check，
保证排版侧与校对侧同一份输入得到同一结论。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import format_check
from ..locator import field_locator
from .report import CheckItem

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class GongwenDocument(BaseModel):
    """一篇待排版/待验收的公文。

    除正文外全部可选：写作流产出的是骨架稿，先在缺要素状态下做体检，
    再逐步补齐；强制全字段会让「部分草稿的要素检查」无法进行。
    """

    copy_number: str | None = Field(default=None, max_length=32)  # 份号
    secrecy: str | None = Field(default=None, max_length=64)  # 密级和保密期限
    urgency: str | None = Field(default=None, max_length=16)  # 紧急程度
    issuer_mark: str | None = Field(default=None, max_length=120)  # 发文机关标志
    doc_number: str | None = Field(default=None, max_length=64)  # 发文字号
    signer: str | None = Field(default=None, max_length=32)  # 签发人
    title: str | None = Field(default=None, max_length=200)  # 标题
    addressee: str | None = Field(default=None, max_length=200)  # 主送机关
    body: str = Field(min_length=1)  # 正文（\n 分段）
    attachment_note: str | None = Field(default=None, max_length=2000)  # 附件说明
    attachments: list[str] = Field(default_factory=list)  # 附件清单
    issuer_signature: str | None = Field(default=None, max_length=120)  # 发文机关署名
    date: str | None = Field(default=None, max_length=32)  # 成文日期
    seal: bool | None = None  # 是否加盖印章（None = 未说明）
    note: str | None = Field(default=None, max_length=500)  # 附注
    cc: str | list[str] | None = None  # 抄送机关
    printing_org: str | None = Field(default=None, max_length=120)  # 印发机关
    printing_date: str | None = Field(default=None, max_length=32)  # 印发日期


# 无主送机关的文种（公告/通告/命令/公报按体例直接面向社会或下级机关）
NO_ADDRESSEE_DOCTYPES: frozenset[str] = frozenset({"公告", "通告", "命令", "公报"})

# (字段名, 中文标签, GB 条款)
REQUIRED_ELEMENTS: tuple[tuple[str, str, str], ...] = (
    ("title", "标题", "GB/T 9704-2012 8.1 标题"),
    ("addressee", "主送机关", "GB/T 9704-2012 8.2 主送机关"),
    ("body", "正文", "GB/T 9704-2012 8.3 正文"),
    ("issuer_signature", "发文机关署名", "GB/T 9704-2012 8.5 发文机关署名、成文日期和印章"),
    ("date", "成文日期", "GB/T 9704-2012 8.5 发文机关署名、成文日期和印章"),
)

# 条件性要素：缺了只告警（P2），绝不判 P0 —— 见模块 docstring 的误报论证
OPTIONAL_ELEMENTS: tuple[tuple[str, str, str], ...] = (
    ("copy_number", "份号", "GB/T 9704-2012 7.1 份号"),
    ("secrecy", "密级和保密期限", "GB/T 9704-2012 7.2 密级和保密期限"),
    ("urgency", "紧急程度", "GB/T 9704-2012 7.3 紧急程度"),
    ("issuer_mark", "发文机关标志", "GB/T 9704-2012 7.4 发文机关标志"),
    ("signer", "签发人", "GB/T 9704-2012 7.6 签发人"),
    ("attachment_note", "附件说明", "GB/T 9704-2012 8.4 附件说明"),
    ("seal", "印章", "GB/T 9704-2012 8.5 发文机关署名、成文日期和印章"),
    ("note", "附注", "GB/T 9704-2012 8.6 附注"),
    ("attachments", "附件", "GB/T 9704-2012 8.7 附件"),
    ("cc", "抄送机关", "GB/T 9704-2012 9.1 抄送机关"),
    ("printing_org", "印发机关", "GB/T 9704-2012 9.2 印发机关和印发日期"),
    ("printing_date", "印发日期", "GB/T 9704-2012 9.2 印发机关和印发日期"),
)

# 18 项国标要素清单（逐项对照用）。页码由渲染器固定生成，发布机关与日期合成一项。
_CHECKLIST: tuple[tuple[str, str, bool, str], ...] = (
    ("copy_number", "份号", False, "GB/T 9704-2012 7.1 份号"),
    ("secrecy", "密级和保密期限", False, "GB/T 9704-2012 7.2 密级和保密期限"),
    ("urgency", "紧急程度", False, "GB/T 9704-2012 7.3 紧急程度"),
    ("issuer_mark", "发文机关标志", False, "GB/T 9704-2012 7.4 发文机关标志"),
    ("doc_number", "发文字号", False, "GB/T 9704-2012 7.5 发文字号"),
    ("signer", "签发人", False, "GB/T 9704-2012 7.6 签发人"),
    ("title", "标题", True, "GB/T 9704-2012 8.1 标题"),
    ("addressee", "主送机关", True, "GB/T 9704-2012 8.2 主送机关"),
    ("body", "正文", True, "GB/T 9704-2012 8.3 正文"),
    ("attachment_note", "附件说明", False, "GB/T 9704-2012 8.4 附件说明"),
    ("issuer_signature", "发文机关署名", True, "GB/T 9704-2012 8.5 发文机关署名、成文日期和印章"),
    ("date", "成文日期", True, "GB/T 9704-2012 8.5 发文机关署名、成文日期和印章"),
    ("seal", "印章", False, "GB/T 9704-2012 8.5 发文机关署名、成文日期和印章"),
    ("note", "附注", False, "GB/T 9704-2012 8.6 附注"),
    ("attachments", "附件", False, "GB/T 9704-2012 8.7 附件"),
    ("cc", "抄送机关", False, "GB/T 9704-2012 9.1 抄送机关"),
    ("printing_org", "印发机关和印发日期", False, "GB/T 9704-2012 9.2 印发机关和印发日期"),
    ("page_number", "页码", False, "GB/T 9704-2012 10 页码"),
)


def gb_checklist() -> list[dict[str, Any]]:
    """GB/T 9704-2012 的 18 项要素清单（机器可读的逐项对照表）。"""
    return [
        {"key": key, "label": label, "required": required, "gb_reference": ref}
        for key, label, required, ref in _CHECKLIST
    ]


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _present(value: Any) -> bool:
    """要素是否已填写：空串/空白/空列表都算缺失；seal 只看 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    if value is None:
        return False
    return bool(str(value).strip())


def _preview(value: Any, limit: int = 40) -> str:
    text = "，".join(value) if isinstance(value, (list, tuple)) else str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def required_elements(
    doc: GongwenDocument, *, doctype: str | None = None
) -> list[CheckItem]:
    """必需要素与条件性要素的完整性检查。

    主送机关一项按文种放宽：公告/通告/命令/公报按体例可无主送机关，
    此时缺主送只记 P2 告警，不阻断交付。
    """
    items: list[CheckItem] = []

    for key, label, _ref in REQUIRED_ELEMENTS:
        value = getattr(doc, key)
        if _present(value):
            items.append(
                CheckItem(
                    rule_id=f"gov.element.{key}",
                    status="pass",
                    severity="P0",
                    locator=field_locator(key),
                    evidence=f"「{label}」已填写：{_preview(value)}",
                )
            )
            continue
        if key == "addressee" and doctype in NO_ADDRESSEE_DOCTYPES:
            items.append(
                CheckItem(
                    rule_id=f"gov.element.{key}",
                    status="warn",
                    severity="P2",
                    locator=field_locator(key),
                    evidence=f"未填写「{label}」；文种「{doctype}」按体例可不标注主送机关",
                    suggestion="如确需主送，请补全机关全称或规范化简称并以全角冒号收束",
                )
            )
            continue
        items.append(
            CheckItem(
                rule_id=f"gov.element.{key}",
                status="fail",
                severity="P0",
                locator=field_locator(key),
                evidence=f"缺少必需要素「{label}」",
                suggestion=f"补充{label}（GB/T 9704-2012 规定的公文必备要素）",
            )
        )

    for key, label, _ref in OPTIONAL_ELEMENTS:
        value = getattr(doc, key)
        if _present(value):
            detail = (
                f"「{label}」：{'已加盖' if value else '未加盖'}"
                if isinstance(value, bool)
                else f"「{label}」已标注：{_preview(value)}"
            )
            items.append(
                CheckItem(
                    rule_id=f"gov.element.{key}",
                    status="pass",
                    severity="P2",
                    locator=field_locator(key),
                    evidence=detail,
                )
            )
        else:
            items.append(
                CheckItem(
                    rule_id=f"gov.element.{key}",
                    status="warn",
                    severity="P2",
                    locator=field_locator(key),
                    evidence=f"未标注「{label}」；该项按文种/密级/行文方向条件性使用，不阻断交付",
                    suggestion=f"如本公文需要标注{label}，请补齐；否则可忽略本条",
                )
            )
    return items


def validate_elements(doc: GongwenDocument) -> list[CheckItem]:
    """格式类与一致性校验：发文字号、成文日期、印章、附件说明。"""
    items: list[CheckItem] = []

    # 发文字号：六角括号〔〕、无「第」字、序号不编虚位（GB/T 9704-2012 7.5）
    if _present(doc.doc_number):
        fc = format_check.check_doc_number(doc.doc_number or "")
        items.append(
            CheckItem(
                rule_id=fc.rule_id,
                status="pass" if fc.ok else "fail",
                severity="P0",
                locator=field_locator("doc_number"),
                evidence=f"发文字号「{fc.value}」：{fc.reason or '格式合法'}",
                suggestion=None if fc.ok else fc.suggestion,
            )
        )
    else:
        # 纪要类可不标发文字号，故缺席只告警（与 required_elements 的口径一致）
        items.append(
            CheckItem(
                rule_id=format_check.RULE_DOC_NUMBER,
                status="warn",
                severity="P2",
                locator=field_locator("doc_number"),
                evidence="未填写发文字号；纪要类公文可不标注，其余文种应当标注",
                suggestion="机关代字〔年份〕序号号，如：国发〔2024〕5号",
            )
        )

    # 成文日期：YYYY年M月D日，月日不编虚位（GB/T 9704-2012 8.5）
    # 缺席已由 required_elements 判 P0，这里不重复报。
    if _present(doc.date):
        fc = format_check.check_document_date(doc.date or "")
        items.append(
            CheckItem(
                rule_id=fc.rule_id,
                status="pass" if fc.ok else "fail",
                severity="P0",
                locator=field_locator("date"),
                evidence=f"成文日期「{fc.value}」：{fc.reason or '格式合法'}",
                suggestion=None if fc.ok else fc.suggestion,
            )
        )

    # 印章一致性：有署名和日期而明确未盖章 → P1（特定普发性公文与电报可不盖章）
    if (
        doc.seal is False
        and _present(doc.issuer_signature)
        and _present(doc.date)
    ):
        items.append(
            CheckItem(
                rule_id="gov.element.seal_consistency",
                status="warn",
                severity="P1",
                locator=field_locator("seal"),
                evidence="已有发文机关署名与成文日期，但印章标记为未加盖",
                suggestion="加盖发文机关印章并与署名机关相符；有特定发文机关标志的普发性公文和电报可不盖章",
            )
        )

    # 附件说明 ↔ 附件清单一致性（GB/T 9704-2012 8.4/8.7）
    names = [str(a).strip() for a in doc.attachments if str(a).strip()]
    note = (doc.attachment_note or "").strip()
    if names and not note:
        items.append(
            CheckItem(
                rule_id="gov.element.attachment_mismatch",
                status="fail",
                severity="P1",
                locator=field_locator("attachments"),
                evidence=f"附件清单有 {len(names)} 项（{_preview(names)}），但正文缺少附件说明",
                suggestion="在正文下空一行左空二字标注「附件：1.名称」",
            )
        )
    elif note and not names:
        items.append(
            CheckItem(
                rule_id="gov.element.attachment_mismatch",
                status="fail",
                severity="P1",
                locator=field_locator("attachment_note"),
                evidence=f"有附件说明「{_preview(note)}」，但未提供附件清单",
                suggestion="补充附件清单，或删除附件说明",
            )
        )
    elif names and note:
        absent = [name for name in names if name not in note]
        items.append(
            CheckItem(
                rule_id="gov.element.attachment_mismatch",
                status="fail" if absent else "pass",
                severity="P1",
                locator=field_locator("attachments"),
                evidence=(
                    f"附件说明未覆盖附件清单：{_preview(absent)}"
                    if absent
                    else f"附件说明与附件清单一致（{len(names)} 项）"
                ),
                suggestion=(
                    "在附件说明中按顺序补列：" + "、".join(absent) if absent else None
                ),
            )
        )

    return items
