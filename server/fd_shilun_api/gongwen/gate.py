"""成稿交付闸门（task 5.1）：排版验收 + 规则校对，双闸合一。

规范不变量（shilun-serious-writing「公文成稿交付」）：
存在 P0 级问题的成稿 SHALL NOT 被标记为可交付，且 MUST 列出问题与定位。

三个闸门各管一段，缺一段都不算完整：
- 校对闸（proofread）：P0 由确定性规则产生（要素缺失、发文字号/成文日期格式、
  敏感词），P1/P2 是建议与条件性告警，不该拦住一份合规公文；
- 引用闸（verify）：成稿里的引用或范式依据继承三级核验（spec「成稿引用继承
  三级核验」），enforce 下把被 block 的引用从正文剥除，shadow 下只报告；
- 排版闸（deliver.render）：版式实测、字体回退、DOCX/PDF 产出。

判定口径：`deliverable` 只在**任一闸门出现 P0 失败**时为 False。P1/P2 落成
`warn` 项随报告返回，既不阻断也不隐藏——用户能看到「这稿能交，但有 4 条建议」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..locator import Locator
from ..proofread.engine import ProofreadResult, proofread
from ..verify.service import verify_text, verification_mode
from . import deliver
from .elements import GongwenDocument
from .fonts import FontResolution
from .report import AcceptanceReport

GATE_VERSION = "gongwen-delivery-gate-v1"

Origin = Literal["proofread", "citation", "typography"]
GateStatus = Literal["fail", "warn"]

# 要素类 P0：缺要素、发文字号格式、成文日期格式。排版验收的 elements 侧已逐项报出，
# 闸门若再收一遍校对的同类问题，同一个缺陷会让 P0 计数翻倍。两侧共用
# format_check 的判定，因此这里只做「不重复计入」的取舍，不改变任何判定。
# 必须与 gongwen/deliver.py 的 _ELEMENT_OWNED_P0_RULES 保持一致。
ELEMENT_OWNED_P0_RULES = frozenset(
    {"pf.p0.missing_element", "pf.p0.doc_number_format", "pf.p0.date_format"}
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GateItem(BaseModel):
    """一条阻断/告警项：来自哪个闸门、哪条规则、什么级别、在哪、凭什么。"""

    origin: Origin
    rule_id: str
    status: GateStatus
    severity: Literal["P0", "P1", "P2"]
    locator: Locator
    evidence: str
    suggestion: str | None = None


class GateReport(BaseModel):
    """一次交付判定的完整结论（可机读，可逐条复核）。"""

    schema_version: str = GATE_VERSION
    generated_at: str = Field(default_factory=_now)
    deliverable: bool
    p0_fail: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    proofread_counts: dict[str, int] = Field(default_factory=dict)
    citation_counts: dict[str, int] = Field(default_factory=dict)
    citation_mode: str | None = None
    typography_counts: dict[str, int] = Field(default_factory=dict)
    stripped_citation_ids: list[str] = Field(default_factory=list)
    items: list[GateItem] = Field(default_factory=list)
    blocked_reason: str | None = None

    @property
    def blocking_items(self) -> list[GateItem]:
        return [i for i in self.items if i.status == "fail" and i.severity == "P0"]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class GateOutcome(BaseModel):
    """判定 + 交付物 + 字体决策。不可交付时 docx/pdf 恒为 None。"""

    report: GateReport
    docx: bytes | None = None
    pdf: bytes | None = None
    fonts: FontResolution | None = None
    proofread: ProofreadResult | None = None
    acceptance: AcceptanceReport | None = None
    citation_report: dict[str, Any] | None = None

    model_config = {"arbitrary_types_allowed": True}


def _doc_fields(doc: GongwenDocument) -> dict[str, str | None]:
    """公文要素 → 校对引擎的字段表（元素/格式规则据此判定）。"""
    return {
        "title": doc.title,
        "addressee": doc.addressee,
        "issuer_signature": doc.issuer_signature,
        "date": doc.date,
        "doc_number": doc.doc_number,
        "cc": doc.cc,
    }


def _from_proofread(result: ProofreadResult) -> list[GateItem]:
    items: list[GateItem] = []
    for issue in result.issues:
        # 要素类 P0（缺要素/文号/日期格式）由排版验收的 elements 侧报出，
        # 这里再报一次会把 P0 计数翻倍——两处共用 format_check，判定同源。
        if issue.severity == "P0" and issue.rule_id in ELEMENT_OWNED_P0_RULES:
            continue
        # P0 是确定性反证 → fail（阻断）；P1/P2 是可忽略建议 → warn（不阻断）
        items.append(
            GateItem(
                origin="proofread",
                rule_id=issue.rule_id,
                status="fail" if issue.severity == "P0" else "warn",
                severity=issue.severity,
                locator=issue.locator,
                evidence=issue.evidence or issue.message,
                suggestion=issue.suggestion or None,
            )
        )
    return items


def _from_acceptance(report: AcceptanceReport) -> list[GateItem]:
    items: list[GateItem] = []
    for item in report.items:
        if item.status == "pass":
            continue  # 交付闸只列问题；通过项留在排版验收报告里逐条可查
        items.append(
            GateItem(
                origin="typography",
                rule_id=item.rule_id,
                status="fail" if item.status == "fail" else "warn",
                severity=item.severity or "P1",
                locator=item.locator,
                evidence=item.evidence,
                suggestion=item.suggestion,
            )
        )
    return items


def _from_citations(items_: list, *, mode: str, stripped: set[str]) -> list[GateItem]:
    """引用核验结果 → 闸门条目。

    引用问题不走 P0：它们是引用闸自己的规则（enforce 剥除、shadow 观察），
    不该把「有一条待核引用」升级成「整篇公文不可交付」——那会重演误删。
    """
    out: list[GateItem] = []
    for item in items_:
        if item.status == "pass":
            continue
        cid = item.locator.citation_id or ""
        evidence = item.evidence[0].detail if item.evidence else item.reason
        if item.status == "block":
            action = "已在 enforce 模式下从成稿剥除" if cid in stripped else "shadow 模式保留，仅报告"
            evidence = f"{evidence}；{action}"
        out.append(
            GateItem(
                origin="citation",
                rule_id=item.rule_id,
                status="warn",
                severity="P1",
                locator=item.locator,
                evidence=evidence,
                suggestion="补齐元数据/原文，或删除该引用" if item.status == "block" else "人工核对该引用",
            )
        )
    return out


def evaluate(
    doc: GongwenDocument,
    *,
    want_pdf: bool = False,
    strict: bool | None = None,
    ignored_rule_ids: tuple[str, ...] = (),
    with_artifacts: bool = True,
    fonts: FontResolution | None = None,
    citations: list[dict] | None = None,
    citation_mode: str | None = None,
    citation_resolver: Any = None,
) -> GateOutcome:
    """跑三道闸并给出交付判定。

    `with_artifacts=False` 用于「只问能不能交、先别生成文件」的预览场景：
    判定逻辑完全一致，只是不产出 DOCX/PDF。
    `fonts` 显式给出时与 render 共用同一套字体决策（测试注入与部署预检用），
    保证报告里写的族名就是文件里绑定的族名。
    `citations` 给出时，正文里的引用继承三级核验（spec「成稿引用继承三级核验」）：
    enforce 下被 block 的引用在成稿前剥除，shadow 下只报告不动正文。
    """
    body = doc.body or ""
    mode = citation_mode or verification_mode()
    citation_items: list[GateItem] = []
    citation_report: dict[str, Any] | None = None
    stripped_ids: list[str] = []
    if citations is not None or "[[cite:" in body:
        outcome = verify_text(
            body,
            citations or [],
            mode=mode,
            resolver=citation_resolver,
        )
        citation_report = outcome.report.model_dump(mode="json")
        stripped = set(outcome.report.blocked_citation_ids) if mode == "enforce" else set()
        stripped_ids = sorted(stripped)
        citation_items = _from_citations(outcome.report.items, mode=mode, stripped=stripped)
        # enforce 下成稿正文换成剥除后的文本；shadow 下二者本就相同
        body = outcome.cleaned_text
        doc = doc.model_copy(update={"body": body})

    proof = proofread(body, fields=_doc_fields(doc), ignored_rule_ids=ignored_rule_ids)

    acceptance: AcceptanceReport | None = None
    resolution: FontResolution | None = None
    docx: bytes | None = None
    pdf: bytes | None = None
    typography_items: list[GateItem] = []

    if with_artifacts:
        # skip_proofread：校对由本闸门统一跑（带 P1/P2 忽略清单与要素去重），
        # 渲染侧不再重复一遍，避免同一问题在报告里出现两次。
        rendered = deliver.render(
            doc, want_pdf=want_pdf, strict=strict, fonts=fonts, skip_proofread=True
        )
        acceptance = rendered.report
        resolution = rendered.fonts
        docx, pdf = rendered.docx, rendered.pdf
        typography_items = _from_acceptance(rendered.report)

    items = _from_proofread(proof) + citation_items + typography_items
    p0_fail = sum(1 for i in items if i.status == "fail" and i.severity == "P0")
    deliverable = p0_fail == 0

    counts: dict[str, int] = {"total": len(items), "fail": 0, "warn": 0}
    for item in items:
        counts[item.status] += 1

    blocked_reason = None
    if not deliverable:
        rules = list(dict.fromkeys(i.rule_id for i in items if i.status == "fail" and i.severity == "P0"))
        blocked_reason = f"存在 {p0_fail} 项 P0 问题，成稿不可标记为可交付：{', '.join(rules)}"

    report = GateReport(
        deliverable=deliverable,
        p0_fail=p0_fail,
        counts=counts,
        proofread_counts=dict(proof.counts),
        citation_counts=(
            dict(citation_report["counts"]) if citation_report is not None else {}
        ),
        citation_mode=mode if citation_report is not None else None,
        typography_counts=dict(acceptance.counts) if acceptance is not None else {},
        items=items,
        blocked_reason=blocked_reason,
        stripped_citation_ids=stripped_ids,
    )
    # 不可交付就不给交付物：只返回报告，避免下游拿到文件后忽略 deliverable 标记
    if not deliverable:
        docx = pdf = None
    return GateOutcome(
        report=report,
        docx=docx,
        pdf=pdf,
        fonts=resolution,
        proofread=proof,
        acceptance=acceptance,
        citation_report=citation_report,
    )


__all__ = [
    "GATE_VERSION",
    "GateItem",
    "GateOutcome",
    "GateReport",
    "evaluate",
]
