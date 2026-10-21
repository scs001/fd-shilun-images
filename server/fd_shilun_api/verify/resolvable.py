"""一级「可解」核验：引用能否解析到本项目收藏条目。

规范不变量（「严肃文本引用可解」）：无法解析到来源的引用 MUST NOT 出现在
最终文本中（宁删勿假）。本模块只回答「这个 id 在不在本项目引用清单里」，
不查外部来源，因此判定完全确定：同一文本、同一清单必得同一结论。

定位按标记在正文中的字符区间给出（首个出现位置），前端可据此精确高亮到
被拦截的那个 [[cite:id]]。
"""

from __future__ import annotations

import re
from typing import Iterator

from ..locator import citation_locator
from .models import RULE_RESOLVABLE, Evidence, VerificationItem

# 与 writing.resolve_citations 使用同一正则：两处若切出不同标记，
# 「可解」判定与文本剥除会出现口径漂移，故只保留这一份定义。
CITE_MARKER_RE = re.compile(r"\[\[cite:([^\[\]]*?)\]\]")

# Locator.marker 有长度上限；超长畸形标记截断只为可序列化，不影响 id 判定。
_MARKER_LIMIT = 200


def iter_markers(text: str) -> Iterator[tuple[str, int, int]]:
    """按出现顺序产出 (citation_id, char_start, char_end)，id 已去首尾空白。"""
    for m in CITE_MARKER_RE.finditer(text or ""):
        yield m.group(1).strip(), m.start(), m.end()


def verify_resolvable(text: str, valid_ids: set[str]) -> list[VerificationItem]:
    """逐个不同标记判定可解性；同一 id 只出一条结论（取首个出现位置）。

    只产出 pass/block 两态：清单比对没有「不确定」的中间地带——不在清单里
    就是不可解，必须被剥除。
    """
    seen: set[str] = set()
    items: list[VerificationItem] = []
    for cid, start, end in iter_markers(text):
        if cid in seen:
            continue
        seen.add(cid)
        found = cid in valid_ids
        marker = text[start:end][:_MARKER_LIMIT]
        items.append(
            VerificationItem(
                rule_id=RULE_RESOLVABLE,
                level="resolvable",
                status="pass" if found else "block",
                locator=citation_locator(
                    cid, char_start=start, char_end=end, marker=marker
                ),
                evidence=[
                    Evidence(
                        source="collection",
                        detail=(
                            "引用 id 在本项目引用清单中"
                            if found
                            else "引用 id 不在本项目引用清单中"
                        ),
                        fields={"citation_id": cid, "in_collection": found},
                    )
                ],
                reason=(
                    "引用可解"
                    if found
                    else "引用不可解：来源 id 无法解析到本项目收藏条目（宁删勿假）"
                ),
            )
        )
    return items
