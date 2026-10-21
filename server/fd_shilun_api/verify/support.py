"""三级「支持」核验：被引句与数字是否真的来自来源原文。

规范不变量：只对被引原文执行；原文不可得（含仅有摘要）时标 pending_review，
既不得静默放行，也不得静默删除。摘要不是全文——摘要措辞往往与原文不同，
把它当全文会让句级比对产生系统性误判，因此本模块只接受调用方明确传入的
fulltext。

判定完全确定：归一化（空白折叠、全半角统一、拉丁小写）后，中文按字符
二元组 Jaccard、拉丁按词 Jaccard 取最相似句；数字逐个核对是否在原文出现。
同一输入必得同一分数与同一阈值结论。
"""

from __future__ import annotations

import os
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from ..locator import Locator, citation_locator
from .models import (
    RULE_FULLTEXT_MISSING,
    RULE_SUPPORT,
    Evidence,
    VerificationItem,
)
from .resolvable import CITE_MARKER_RE

_DEFAULT_THRESHOLD = 0.34

# 句末标点 + 换行；英文句号只在后接空白时切分，避免拆开 3.5 / e.g. 这类片段。
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?\n]+|(?<=\.)\s+")

# 数字：整数/小数/千分位；% 与单位不参与存在性比对，只认数值本身。
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[a-z]")
_WORD_RE = re.compile(r"[a-z0-9]+")
_COMPACT_RE = re.compile(r"[^\w\u4e00-\u9fff]+")

_PUNCT_MAP = str.maketrans(
    {
        "。": ".",
        "、": ",",
        "；": ";",
        "：": ":",
        "！": "!",
        "？": "?",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "《": "<",
        "》": ">",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "—": "-",
        "–": "-",
        "−": "-",
        "～": "~",
    }
)


def _threshold() -> float:
    """阈值可经 FD_SHILUN_SUPPORT_THRESHOLD 覆盖；非法值回落到默认值。"""
    raw = (os.environ.get("FD_SHILUN_SUPPORT_THRESHOLD") or "").strip()
    if not raw:
        return _DEFAULT_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_THRESHOLD
    return min(max(value, 0.0), 1.0)


def _normalize(text: str) -> str:
    """空白折叠、全半角统一、中文标点转 ASCII、拉丁小写。"""
    s = unicodedata.normalize("NFKC", text or "").translate(_PUNCT_MAP).casefold()
    return re.sub(r"\s+", " ", s).strip()


def _strip_markers(text: str) -> str:
    """标记本身不是断言内容，比对前去掉，避免 [[cite:x]] 污染相似度。"""
    return CITE_MARKER_RE.sub("", text or "")


def _canon_number(token: str) -> str | None:
    """数值归一：千分位去掉，整数与等价小数视为同一数（1000 == 1,000）。"""
    core = token.replace(",", "")
    try:
        value = Decimal(core)
    except (InvalidOperation, ValueError):
        return None
    if value == value.to_integral_value():
        return format(value.to_integral_value(), "f")
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _numbers(text: str) -> list[str]:
    """按出现顺序返回去重后的数值（已归一）。"""
    out: list[str] = []
    for token in _NUMBER_RE.findall(text or ""):
        canon = _canon_number(token)
        if canon is not None and canon not in out:
            out.append(canon)
    return out


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s and s.strip()]


def _single_cjk_heavy(text: str) -> bool:
    cjk = len(_CJK_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    return cjk > 0 and cjk >= latin


def _bigrams(text: str) -> set[str]:
    compact = _COMPACT_RE.sub("", text or "")
    if not compact:
        return set()
    if len(compact) == 1:
        return {compact}
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text or ""))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _similarity(norm_a: str, norm_b: str) -> float:
    """中文重文本用字符二元组，拉丁文本用词集合；两者都是确定性的。"""
    if not norm_a or not norm_b:
        return 0.0
    if _single_cjk_heavy(norm_a) or _single_cjk_heavy(norm_b):
        return _jaccard(_bigrams(norm_a), _bigrams(norm_b))
    return _jaccard(_tokens(norm_a), _tokens(norm_b))


def _item(
    *,
    status: str,
    rule_id: str,
    locator: Locator,
    evidence: list[Evidence],
    reason: str,
) -> VerificationItem:
    return VerificationItem(
        rule_id=rule_id,
        level="support",
        status=status,  # type: ignore[arg-type]
        locator=locator,
        evidence=evidence,
        reason=reason,
    )


def verify_support(
    assertion: str,
    fulltext: str | None,
    *,
    citation_id: str,
    locator: Locator | None = None,
) -> VerificationItem:
    """判定断言是否被原文支持：数字全中 + 最相似句达阈值才算 pass。"""
    loc = locator or citation_locator(citation_id)

    if not (fulltext or "").strip():
        return _item(
            status="pending_review",
            rule_id=RULE_FULLTEXT_MISSING,
            locator=loc,
            evidence=[
                Evidence(
                    source="none",
                    detail="未提供原文全文（摘要不计为全文），句级与数字比对无法执行",
                    fields={"citation_id": citation_id},
                )
            ],
            reason="无原文全文，无法做支持性比对，标注待核（不计入已通过）",
        )

    cleaned = _strip_markers(assertion).strip()
    if not cleaned:
        return _item(
            status="pending_review",
            rule_id=RULE_SUPPORT,
            locator=loc,
            evidence=[
                Evidence(
                    source="none",
                    detail="被引句中除引用标记外没有可核对的断言文本",
                    fields={"citation_id": citation_id},
                )
            ],
            reason="被引句为空，支持性无法判定，标注待核（不计入已通过）",
        )

    norm_assertion = _normalize(cleaned)
    norm_fulltext = _normalize(fulltext)
    threshold = _threshold()

    # 数字先算硬失败：断言里的每个数字都必须能在原文中找到同值出现。
    wanted = _numbers(norm_assertion)
    available = set(_numbers(norm_fulltext))
    missing = [n for n in wanted if n not in available]
    if missing:
        return _item(
            status="block",
            rule_id=RULE_SUPPORT,
            locator=loc,
            evidence=[
                Evidence(
                    source="fulltext",
                    detail=f"断言中的数字未在原文出现：{', '.join(missing)}",
                    fields={
                        "assertion": cleaned,
                        "numbers_in_assertion": wanted,
                        "missing_numbers": missing,
                    },
                )
            ],
            reason=f"断言未被原文支持：数字 {', '.join(missing)} 在原文中找不到",
        )

    best_score = 0.0
    best_sentence = ""
    for sentence in _split_sentences(fulltext):
        score = _similarity(norm_assertion, _normalize(sentence))
        # 严格大于才替换：同分时保留原文中更靠前的句子，保证结果可复现。
        if score > best_score:
            best_score, best_sentence = score, sentence

    fields = {
        "assertion": cleaned,
        "closest_sentence": best_sentence,
        "score": round(best_score, 4),
        "threshold": round(threshold, 4),
        "matched_numbers": wanted,
        "missing_numbers": [],
    }
    if best_score >= threshold:
        return _item(
            status="pass",
            rule_id=RULE_SUPPORT,
            locator=loc,
            evidence=[
                Evidence(
                    source="fulltext",
                    detail=(
                        f"原文最相似句相似度 {best_score:.4f} ≥ 阈值 {threshold:.2f}"
                    ),
                    fields=fields,
                )
            ],
            reason="断言在原文中找到对应语句（数字一致）",
        )

    return _item(
        status="block",
        rule_id=RULE_SUPPORT,
        locator=loc,
        evidence=[
            Evidence(
                source="fulltext",
                detail=(
                    f"原文最相似句相似度 {best_score:.4f} 低于阈值 {threshold:.2f}"
                    "；数字命中不等于断言被支持"
                ),
                fields=fields,
            )
        ],
        reason=(
            f"断言未被原文支持：最相似句相似度 {best_score:.4f} 低于阈值 {threshold:.2f}"
        ),
    )
