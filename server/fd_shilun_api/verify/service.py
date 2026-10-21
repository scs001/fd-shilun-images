"""三级引用核验编排：可解 → 真实 → 支持，并据此处置文本。

规范不变量（「严肃文本引用可解」）：
- block（不可解 / 确认不存在 / 元数据不符 / 已撤稿 / 原文不支持）引用
  MUST NOT 出现在最终文本中；
- pending_review（来源未覆盖 / 无全文 / 上游失联）SHALL 保留并标注待核，
  SHALL NOT 以「无法确定」为由静默删除，也不得计入已通过；
- off 与 shadow 只产出报告、不改文本（逐字节一致），enforce 才剥除 block
  标记；pending 在任何模式下都不改文本。

三级只在引用已解（一级 pass）后执行。三级之间互不短路：一条引用可能同时
「上游失联」（二级 pending）与「无全文」（三级 pending），报告逐条留痕，
但文本处置只看 block。
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from ..locator import Locator, citation_locator
from .authentic import HttpMetadataResolver, MetadataResolver, verify_authentic
from .models import (
    RULE_UPSTREAM,
    Evidence,
    VerificationItem,
    VerificationReport,
    build_report,
)
from .resolvable import CITE_MARKER_RE, iter_markers, verify_resolvable
from .support import verify_support

VerificationMode = Literal["off", "shadow", "enforce"]
_MODES = ("off", "shadow", "enforce")

# 句末标点与换行：与三级断言取句口径一致。
_SENTENCE_END_RE = re.compile(r"[。！？!?\n]")

# 这些来源类型不在公开元数据通道覆盖范围内。对它们跑二级查询会把
# 「来源未覆盖」误判成「来源不存在」，因此直接标待核（决策 2）。
_UNCOVERED_KINDS = {"gov_corpus"}


class Annotation(BaseModel):
    """待核标注：保留在稿中，供界面展示原因。"""

    citation_id: str
    reason: str
    rule_ids: list[str] = Field(default_factory=list)
    levels: list[str] = Field(default_factory=list)


class VerifyOutcome(BaseModel):
    """一次核验的产出：可审计报告 + 按模式处置后的文本 + 待核标注。"""

    report: VerificationReport
    cleaned_text: str
    annotations: list[Annotation] = Field(default_factory=list)


def verification_mode() -> VerificationMode:
    """读取核验模式；缺省与未知值一律回落 shadow（上线先观察，不改稿）。"""
    raw = (os.environ.get("FD_SHILUN_CITE_VERIFY_MODE") or "").strip().lower()
    return cast(VerificationMode, raw if raw in _MODES else "shadow")


def _normalize_mode(mode: str | None) -> VerificationMode:
    if mode is None:
        return verification_mode()
    m = str(mode).strip().lower()
    return cast(VerificationMode, m if m in _MODES else "shadow")


def _sentence_at(text: str, pos: int) -> str:
    """取包含 pos 的句子（按 。！？!? 与换行切分）。"""
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        if match.start() >= pos:
            return text[start : match.start()]
        start = match.end()
    return text[start:]


def _not_covered_item(kind: str, locator: Locator) -> VerificationItem:
    return VerificationItem(
        rule_id=RULE_UPSTREAM,
        level="authentic",
        status="pending_review",
        locator=locator,
        evidence=[
            Evidence(
                source="none",
                detail=(
                    f"来源类型 {kind} 不在 Crossref/OpenAlex/DataCite/PubMed "
                    "覆盖范围内，无法自动核对真实性"
                ),
                fields={"kind": kind},
            )
        ],
        reason="来源未覆盖，无法完成真实性核验，标注待核（不计入已通过）",
    )


def _annotations(report: VerificationReport) -> list[Annotation]:
    """按待核 id 归并原因：同一引用可能同时缺上游与缺全文。"""
    pending_ids = report.pending_citation_ids
    if not pending_ids:
        return []
    pending_set = set(pending_ids)
    reasons: dict[str, list[str]] = {cid: [] for cid in pending_ids}
    rules: dict[str, list[str]] = {cid: [] for cid in pending_ids}
    levels: dict[str, list[str]] = {cid: [] for cid in pending_ids}
    for item in report.items:
        cid = item.locator.citation_id
        if item.status != "pending_review" or cid not in pending_set:
            continue
        if item.reason and item.reason not in reasons[cid]:
            reasons[cid].append(item.reason)
        if item.rule_id not in rules[cid]:
            rules[cid].append(item.rule_id)
        if item.level not in levels[cid]:
            levels[cid].append(item.level)
    return [
        Annotation(
            citation_id=cid,
            reason="；".join(reasons[cid]) or "待核",
            rule_ids=rules[cid],
            levels=levels[cid],
        )
        for cid in pending_ids
    ]


def _pick_fulltext(
    citation_id: str, meta: dict, fulltexts: dict[str, str] | None
) -> str | None:
    """原文只认显式 fulltext；摘要（abstract）绝不冒充全文。"""
    candidate: Any = None
    if fulltexts:
        candidate = fulltexts.get(citation_id)
    if not (isinstance(candidate, str) and candidate.strip()):
        candidate = meta.get("fulltext")
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    return None


def verify_text(
    text: str,
    citations: list[dict],
    *,
    mode: str | None = None,
    resolver: MetadataResolver | None = None,
    fulltexts: dict[str, str] | None = None,
) -> VerifyOutcome:
    """对正文做三级核验，并按模式返回处置后的文本。"""
    text = text or ""
    resolved_mode = _normalize_mode(mode)

    by_id: dict[str, dict] = {}
    for citation in citations or []:
        cid = str(citation.get("id") or citation.get("citation_id") or "")
        if cid:
            by_id[cid] = citation
    resolved_resolver = resolver if resolver is not None else HttpMetadataResolver()

    # 一级：每个不同标记一条；未解出的不再进入二、三级。
    level1 = verify_resolvable(text, set(by_id))
    items: list[VerificationItem] = list(level1)
    resolved_ids = {item.locator.citation_id for item in level1 if item.status == "pass"}

    seen: set[str] = set()
    for cid, start, end in iter_markers(text):
        if cid not in resolved_ids or cid in seen:
            continue
        seen.add(cid)
        citation = by_id.get(cid) or {}
        meta = citation.get("meta") or {}
        kind = str(citation.get("kind") or "openalex")
        locator = citation_locator(
            cid, char_start=start, char_end=end, marker=text[start:end][:200]
        )

        # 二级：真实性。
        if kind in _UNCOVERED_KINDS:
            items.append(_not_covered_item(kind, locator))
        else:
            items.extend(
                verify_authentic(
                    meta, resolved_resolver, citation_id=cid, locator=locator
                )
            )

        # 三级：断言取标记所在句（引用标记本身不算断言内容）。
        assertion = _sentence_at(text, start)
        items.append(
            verify_support(
                assertion,
                _pick_fulltext(cid, meta, fulltexts),
                citation_id=cid,
                locator=locator,
            )
        )

    # VerificationReport.mode 只接受 shadow/enforce；off 与 shadow 行为一致，
    # 报告中按 shadow 归类，真实开关由调用方返回的 verification_mode 表达。
    report_mode: Literal["shadow", "enforce"] = (
        "enforce" if resolved_mode == "enforce" else "shadow"
    )
    report = build_report(items, mode=report_mode, citation_count=len(level1))

    removed: set[str] = {
        item.locator.citation_id
        for item in items
        if item.level == "resolvable" and item.status == "block"
    }
    if resolved_mode == "enforce":
        removed |= set(report.blocked_citation_ids)

    def _keep(match: re.Match) -> str:
        return "" if match.group(1).strip() in removed else match.group(0)

    cleaned = CITE_MARKER_RE.sub(_keep, text)
    return VerifyOutcome(report=report, cleaned_text=cleaned, annotations=_annotations(report))
