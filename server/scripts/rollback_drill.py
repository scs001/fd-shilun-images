#!/usr/bin/env python3
"""enforce 切换与回滚演练（task 5.3）。

做什么
------
在一个进程内完整演练「上线 → 出事 → 回滚」三步，用同一份稿件对比：

1. shadow（上线态）：只产报告，正文与历史行为逐字节一致；
2. enforce（切闸）：block 的引用被剥除，pending 保留并标注；
3. shadow（回滚）：正文恢复与第 1 步完全一致。

全部通过则打印 PASS，并以 0 退出；任一不变量破坏则 FAIL 并以 1 退出。

为什么要在同一个进程里跑
------------------------
真实回滚靠改环境变量 + 重启，验证周期长且容易把「重启生效了吗」和
「代码对不对」混为一谈。这里把开关做成显式入参，先证明「开关切回来行为
就一定恢复」，再让运维按同一开关操作环境变量——两步解耦。

用法
----
    python3 scripts/rollback_drill.py
    python3 scripts/rollback_drill.py --verbose
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fd_shilun_api.verify.authentic import (  # noqa: E402
    LookupResult,
    MetadataRecord,
    MetadataResolver,
)
from fd_shilun_api.verify.service import verification_mode, verify_text  # noqa: E402
from fd_shilun_api.writing import resolve_citations  # noqa: E402

ENV_KEY = "FD_SHILUN_CITE_VERIFY_MODE"


class DrillResolver(MetadataResolver):
    """确定性替身：c1 元数据一致（pass）、c2 已撤稿（block）、c3 上游失联（pending）。

    回滚演练必须可复现，因此不用真网络：上游行为固定成三种典型态。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup(self, meta: dict) -> LookupResult:
        doi = str(meta.get("doi") or "")
        self.calls.append(doi)
        if doi == "10.1000/retracted":
            record = MetadataRecord(
                source="crossref",
                title=str(meta.get("title") or ""),
                doi=doi,
                year=meta.get("year"),
                authors=list(meta.get("authors") or []),
                retracted=True,
            )
            return LookupResult(records=[record], responded=["crossref"])
        if doi == "10.1000/ok":
            record = MetadataRecord(
                source="crossref",
                title=str(meta.get("title") or ""),
                doi=doi,
                year=meta.get("year"),
                authors=list(meta.get("authors") or []),
            )
            return LookupResult(records=[record], responded=["crossref"])
        return LookupResult(records=[], responded=[], failures=["openalex: timeout"])


DRILL_TEXT = (
    "第一句引用可核 [[cite:c1]]。"
    "第二句引用已撤稿 [[cite:c2]]。"
    "第三句引用上游失联 [[cite:c3]]。"
    "第四句引用不可解 [[cite:ghost]]。"
)

DRILL_CITATIONS = [
    {
        "id": "c1",
        "kind": "openalex",
        "ref_id": "W1",
        "meta": {"doi": "10.1000/ok", "title": "可核文献", "year": 2023, "authors": ["张三"]},
    },
    {
        "id": "c2",
        "kind": "openalex",
        "ref_id": "W2",
        "meta": {
            "doi": "10.1000/retracted",
            "title": "已撤稿文献",
            "year": 2022,
            "authors": ["李四"],
        },
    },
    {
        "id": "c3",
        "kind": "openalex",
        "ref_id": "W3",
        "meta": {"doi": "10.1000/unknown", "title": "上游失联文献", "year": 2021},
    },
]


def _check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "✓" if condition else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    return condition


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="enforce 切换与回滚演练（task 5.3）")
    parser.add_argument("--verbose", action="store_true", help="打印每次核验的正文")
    args = parser.parse_args(argv)

    resolver = DrillResolver()
    valid_ids = {c["id"] for c in DRILL_CITATIONS}
    ok = True

    print("=" * 62)
    print("enforce 切换与回滚演练")
    print("=" * 62)
    print(f"稿件：{DRILL_TEXT}")
    print()

    # ---- 第 1 步：shadow（上线态） ----
    print("[1/3] shadow 上线态（只产报告，不改稿）")
    shadow_before = verify_text(DRILL_TEXT, DRILL_CITATIONS, mode="shadow", resolver=resolver)
    legacy = resolve_citations(DRILL_TEXT, valid_ids)["cleaned_text"]
    ok &= _check(
        "与历史行为逐字节一致（只剥不可解标记）",
        shadow_before.cleaned_text == legacy,
    )
    ok &= _check(
        "报告中出现 block 与 pending（闸门在观察但不处置）",
        shadow_before.report.counts.blocked > 0 and shadow_before.report.counts.pending > 0,
        f"pass={shadow_before.report.counts.passed} "
        f"block={shadow_before.report.counts.blocked} "
        f"pending={shadow_before.report.counts.pending}",
    )
    ok &= _check(
        "撤稿引用仍在正文中（shadow 不剥）",
        "[[cite:c2]]" in shadow_before.cleaned_text,
    )
    if args.verbose:
        print(f"      shadow 正文：{shadow_before.cleaned_text}")

    # ---- 第 2 步：enforce（切闸） ----
    print("\n[2/3] 切 enforce（block 剥除，pending 保留并标注）")
    enforce = verify_text(DRILL_TEXT, DRILL_CITATIONS, mode="enforce", resolver=resolver)
    ok &= _check("撤稿引用被剥除", "[[cite:c2]]" not in enforce.cleaned_text)
    ok &= _check(
        "待核引用保留（不以无法确定为由删除）",
        "[[cite:c3]]" in enforce.cleaned_text,
    )
    ok &= _check("可核引用保留", "[[cite:c1]]" in enforce.cleaned_text)
    ok &= _check("不可解引用仍被剥除（历史行为不变）", "[[cite:ghost]]" not in enforce.cleaned_text)
    ok &= _check(
        "产出待核标注（附原因）",
        len(enforce.annotations) > 0,
        f"{len(enforce.annotations)} 条标注",
    )
    ok &= _check("enforce 与 shadow 正文确实不同（闸门生效）", enforce.cleaned_text != shadow_before.cleaned_text)
    if args.verbose:
        print(f"      enforce 正文：{enforce.cleaned_text}")

    # ---- 第 3 步：回滚到 shadow ----
    print("\n[3/3] 回滚 shadow（正文恢复）")
    shadow_after = verify_text(DRILL_TEXT, DRILL_CITATIONS, mode="shadow", resolver=resolver)
    ok &= _check(
        "正文与回滚前逐字节一致",
        shadow_after.cleaned_text == shadow_before.cleaned_text,
    )
    ok &= _check("正文与历史行为一致", shadow_after.cleaned_text == legacy)
    ok &= _check(
        "撤稿引用回到正文（回滚彻底，无残留剥除）",
        "[[cite:c2]]" in shadow_after.cleaned_text,
    )
    ok &= _check(
        "报告仍产出（回滚不丢可观测性）",
        shadow_after.report.counts.total == shadow_before.report.counts.total,
    )

    # ---- 环境变量开关（运维真实操作面） ----
    print("\n[附加] 环境变量开关 " + ENV_KEY)
    original = os.environ.get(ENV_KEY)
    try:
        os.environ.pop(ENV_KEY, None)
        ok &= _check("缺省即 shadow（上线先观察，不改稿）", verification_mode() == "shadow")
        os.environ[ENV_KEY] = "enforce"
        ok &= _check("设为 enforce 生效", verification_mode() == "enforce")
        os.environ[ENV_KEY] = "shadow"
        ok &= _check("切回 shadow 生效（回滚开关可用）", verification_mode() == "shadow")
        os.environ[ENV_KEY] = "bogus"
        ok &= _check("非法值回落 shadow（不因手误放开闸门）", verification_mode() == "shadow")
    finally:
        os.environ.pop(ENV_KEY, None)
        if original is not None:
            os.environ[ENV_KEY] = original

    print()
    print("=" * 62)
    print("结果：PASS —— enforce 可切、可回滚，回滚后行为完全恢复" if ok else "结果：FAIL —— 存在不变量被破坏，禁止上线")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
