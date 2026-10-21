#!/usr/bin/env python3
"""历史稿件 shadow 回放（task 5.2）：产出 block / pending / pass 比例与误报统计。

用途
----
在把引用核验闸门从 shadow 切到 enforce 之前，用真实历史稿件回放，量化：
- 三态比例（pass / block / pending_review）
- 逐规则命中分布（哪条规则在拦）
- 疑似误报候选（pending 中的「上游失联」比例、block 中的元数据不符明细）

输入
----
1. ``data/gov_corpus.json`` 里的 212 篇真实公文（默认）——用其正文与文号；
2. 或 ``--manuscripts <path>`` 指定的 JSON：``[{"id":..., "text":..., "citations":[...]}]``。

引用来源
--------
历史公文语料本身不含 ``[[cite:...]]`` 标记，回放时按两种方式构造引用：

- ``--mode corpus``（默认）：把语料自带的 ``cited_docs`` / ``doc_number`` 当作
  引用条目（gov_corpus 类型，走「来源未覆盖 → pending」路径）；
- ``--mode synthetic``：按正文自动插入合成引用标记，用于压测三级链路。

输出
----
JSON 报告 + 人读摘要。默认写到 ``--out``（缺省 stdout）。

用法
----
    python3 scripts/replay_shadow.py                      # 回放内置公文语料
    python3 scripts/replay_shadow.py --limit 20 -o /tmp/replay.json
    python3 scripts/replay_shadow.py --mode synthetic --limit 50
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fd_shilun_api.verify.authentic import LookupResult, MetadataResolver  # noqa: E402
from fd_shilun_api.verify.models import (  # noqa: E402
    RULE_FULLTEXT_MISSING,
    RULE_METADATA,
    RULE_RESOLVABLE,
    RULE_RETRACTED,
    RULE_UPSTREAM,
)
from fd_shilun_api.verify.service import verify_text  # noqa: E402
from fd_shilun_api.writing import resolve_citations  # noqa: E402


class OfflineResolver(MetadataResolver):
    """离线解析器：不发任何网络请求，所有二级查询都归为「上游失联」。

    回放用它的原因：历史稿件回放不应依赖外网可用性，也不应把「此刻网络不通」
    混进「稿件本身有问题」的统计里。要测真实上游，改用 --online。
    """

    def __init__(self) -> None:
        self.calls = 0

    def lookup(self, meta: dict) -> LookupResult:
        self.calls += 1
        return LookupResult(records=[], responded=[], failures=["offline: 回放模式不访问外网"])


def load_gov_corpus(limit: int | None) -> list[dict[str, Any]]:
    path = ROOT / "data" / "gov_corpus.json"
    if not path.exists():
        raise SystemExit(f"找不到语料：{path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    docs = payload.get("docs") or []
    if limit is not None:
        docs = docs[:limit]
    return docs


def corpus_text(doc: dict[str, Any]) -> str:
    """取公文正文：sections[].text 拼接；无 sections 时退回 title。"""
    sections = doc.get("sections") or []
    parts = [
        str(s.get("text") or "")
        for s in sections
        if isinstance(s, dict) and str(s.get("text") or "").strip()
    ]
    text = "\n".join(parts).strip()
    return text or str(doc.get("title") or "")


def corpus_citations(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """把语料自带的引用线索转成 citation 条目（gov_corpus 类型）。"""
    citations: list[dict[str, Any]] = []
    for i, cited in enumerate(doc.get("cited_docs") or []):
        if isinstance(cited, str):
            ref, title = cited, cited
        elif isinstance(cited, dict):
            ref = str(cited.get("doc_number") or cited.get("id") or f"cited-{i}")
            title = str(cited.get("title") or ref)
        else:
            continue
        citations.append(
            {
                "id": f"c{i}",
                "kind": "gov_corpus",
                "ref_id": ref,
                "meta": {"title": title, "doc_number": ref},
            }
        )
    return citations


def synthetic_citations(text: str) -> tuple[str, list[dict[str, Any]]]:
    """在正文句末插入合成引用标记，用于压测三级链路。

    每 3 句插一条；标记 id 为 s0/s1/...；引用条目模拟 OpenAlex 元数据，
    其中约 1/4 故意构造为不可解（不放进 citations），验证一级拦截路径。
    """
    sentences = [s for s in text.replace("\n", "。").split("。") if s.strip()]
    citations: list[dict[str, Any]] = []
    out: list[str] = []
    cite_index = 0
    for i, sentence in enumerate(sentences):
        out.append(sentence)
        if i % 3 == 2:
            cid = f"s{cite_index}"
            cite_index += 1
            # 每 4 条留 1 条不可解（不加入 citations）
            if cite_index % 4 != 0:
                citations.append(
                    {
                        "id": cid,
                        "kind": "openalex",
                        "ref_id": f"W{1000000 + cite_index}",
                        "meta": {
                            "title": f"合成文献 {cite_index}",
                            "doi": f"10.9999/synthetic.{cite_index}",
                            "year": 2023,
                        },
                    }
                )
            out.append(f"[[cite:{cid}]]")
    return "。".join(out), citations


def replay(
    manuscripts: list[dict[str, Any]], *, mode: str, resolver: MetadataResolver | None
) -> dict[str, Any]:
    """逐稿核验，汇总三态比例与规则分布。"""
    per_doc: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    rule_counts: Counter[str] = Counter()
    level_counts: Counter[str] = Counter()
    pending_reasons: Counter[str] = Counter()
    blocked_reasons: Counter[str] = Counter()
    errors: list[str] = []
    # 误报统计分桶
    pending_env = 0  # 环境性待核：上游失联，与稿件无关
    pending_structural = 0  # 结构性待核：无全文，覆盖现实
    pending_other = 0
    blocked_unresolvable = 0  # 一级不可解（真实缺陷）
    blocked_metadata = 0  # 元数据不符（真实缺陷）
    blocked_retracted = 0  # 撤稿（真实缺陷）
    blocked_other = 0
    docs_affected = 0  # 切 enforce 会改动正文的稿件数

    for doc in manuscripts:
        doc_id = str(doc.get("doc_id") or doc.get("id") or "unknown")
        try:
            if mode == "synthetic":
                text, citations = synthetic_citations(corpus_text(doc))
            else:
                text = corpus_text(doc)
                citations = corpus_citations(doc)
                # corpus 模式：语料无标记时也插一条，保证闸门被真正走到
                if citations and "[[cite:" not in text:
                    text = text + " [[cite:c0]]"
        except Exception as exc:  # noqa: BLE001 —— 单稿失败不中断整轮回放
            errors.append(f"{doc_id}: {type(exc).__name__}: {exc}")
            continue

        if not text.strip():
            errors.append(f"{doc_id}: 正文为空，跳过")
            continue

        try:
            outcome = verify_text(text, citations, mode="shadow", resolver=resolver)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{doc_id}: 核验失败 {type(exc).__name__}: {exc}")
            continue

        # shadow 的正确不变量：与历史行为（只剥不可解标记）逐字节一致。
        # 一级「可解」拦截本来就发生在渲染层，shadow 不该多剥任何 block。
        legacy = resolve_citations(text, {str(c["id"]) for c in citations})["cleaned_text"]
        matches_legacy = outcome.cleaned_text == legacy

        report = outcome.report
        status_counts["pass"] += report.counts.passed
        status_counts["block"] += report.counts.blocked
        status_counts["pending_review"] += report.counts.pending
        for item in report.items:
            rule_counts[item.rule_id] += 1
            level_counts[item.level] += 1
            if item.status == "pending_review":
                pending_reasons[item.rule_id] += 1
                # 误报分析：待核分两类——环境性（上游失联，与稿件无关）
                # 与结构性（来源未覆盖，中文/公文语料的既有现实）。
                if item.rule_id == RULE_UPSTREAM:
                    pending_env += 1
                elif item.rule_id == RULE_FULLTEXT_MISSING:
                    pending_structural += 1
                else:
                    pending_other += 1
            elif item.status == "block":
                blocked_reasons[item.rule_id] += 1
                # block 分两类：确定性反证（真实缺陷）与疑似误报候选
                if item.rule_id == RULE_RESOLVABLE:
                    blocked_unresolvable += 1
                elif item.rule_id == RULE_METADATA:
                    blocked_metadata += 1
                elif item.rule_id == RULE_RETRACTED:
                    blocked_retracted += 1
                else:
                    blocked_other += 1

        # enforce 演练：只在 block 会改变文本，量化「切 enforce 会动多少稿」
        enforce_outcome = verify_text(text, citations, mode="enforce", resolver=resolver)
        would_change = enforce_outcome.cleaned_text != outcome.cleaned_text
        if would_change:
            docs_affected += 1

        per_doc.append(
            {
                "doc_id": doc_id,
                "title": doc.get("title"),
                "doc_number": doc.get("doc_number"),
                "citation_count": report.citation_count,
                "counts": report.counts.model_dump(),
                "blocked_ids": list(report.blocked_citation_ids),
                "pending_ids": list(report.pending_citation_ids),
                "matches_legacy": matches_legacy,
                "enforce_would_change": would_change,
            }
        )

    total = sum(status_counts.values())
    ratios = {
        k: (round(v / total, 4) if total else None)
        for k, v in (
            ("pass", status_counts["pass"]),
            ("block", status_counts["block"]),
            ("pending_review", status_counts["pending_review"]),
        )
    }
    return {
        "mode": "shadow",
        "replay_mode": mode,
        "documents": len(per_doc),
        "errors": errors,
        "items_total": total,
        "status_counts": dict(status_counts),
        "ratios": ratios,
        "level_counts": dict(level_counts),
        "rule_counts": dict(rule_counts.most_common()),
        "pending_by_rule": dict(pending_reasons.most_common()),
        "blocked_by_rule": dict(blocked_reasons.most_common()),
        # 误报统计：区分「稿件真有问题」与「环境/覆盖限制」
        "false_positive_analysis": {
            "pending_environmental": pending_env,
            "pending_structural_coverage": pending_structural,
            "pending_other": pending_other,
            "blocked_unresolvable": blocked_unresolvable,
            "blocked_metadata_mismatch": blocked_metadata,
            "blocked_retracted": blocked_retracted,
            "blocked_other": blocked_other,
            "note": (
                "pending 不计入已通过，也不视为稿件缺陷；"
                "environmental 是上游失联（重试可消），"
                "structural_coverage 是来源未覆盖（需授权通道补）"
            ),
        },
        "enforce_dry_run": {
            "documents_total": len(per_doc),
            "documents_would_change": docs_affected,
            "change_ratio": (
                round(docs_affected / len(per_doc), 4) if per_doc else None
            ),
            "note": "切 enforce 只影响含 block 的稿件；pending 永不改文本",
        },
        # 回放的核心不变量：shadow 与历史行为（只剥不可解标记）逐字节一致
        "all_matches_legacy": all(d["matches_legacy"] for d in per_doc),
        "per_document": per_doc,
    }


def summarize(report: dict[str, Any]) -> str:
    """人读摘要（终端输出）。"""
    lines = [
        "=" * 62,
        "历史稿件 shadow 回放报告",
        "=" * 62,
        f"回放模式      : {report['replay_mode']}",
        f"稿件数        : {report['documents']}",
        f"核验条目总数  : {report['items_total']}",
        f"shadow=历史行为: {'是' if report['all_matches_legacy'] else '否（异常！）'}",
        "",
        "三态分布：",
    ]
    for key, label in (
        ("pass", "通过"),
        ("block", "拦截"),
        ("pending_review", "待核"),
    ):
        count = report["status_counts"].get(key, 0)
        ratio = report["ratios"].get(key)
        pct = f"{ratio * 100:.1f}%" if ratio is not None else "n/a"
        lines.append(f"  {label:<4} {count:>6} 条   {pct:>7}")
    if report["rule_counts"]:
        lines += ["", "规则命中分布："]
        for rule, count in list(report["rule_counts"].items())[:12]:
            lines.append(f"  {rule:<28} {count:>5}")
    if report["pending_by_rule"]:
        lines += ["", "待核归因（哪条规则导致）："]
        for rule, count in list(report["pending_by_rule"].items())[:8]:
            lines.append(f"  {rule:<28} {count:>5}")
    if report["blocked_by_rule"]:
        lines += ["", "拦截归因："]
        for rule, count in list(report["blocked_by_rule"].items())[:8]:
            lines.append(f"  {rule:<28} {count:>5}")

    # 误报分析
    fp = report.get("false_positive_analysis") or {}
    if fp:
        lines += ["", "误报分析（待核/拦截的性质）："]
        lines.append(
            f"  环境性待核（上游失联，可重试）: {fp.get('pending_environmental', 0):>5}"
        )
        lines.append(
            f"  结构性待核（来源未覆盖）      : {fp.get('pending_structural_coverage', 0):>5}"
        )
        lines.append(f"  一级不可解拦截（真实缺陷）    : {fp.get('blocked_unresolvable', 0):>5}")
        lines.append(f"  元数据不符拦截（真实缺陷）    : {fp.get('blocked_metadata_mismatch', 0):>5}")
        lines.append(f"  撤稿拦截（真实缺陷）          : {fp.get('blocked_retracted', 0):>5}")

    # enforce 演练
    dry = report.get("enforce_dry_run") or {}
    if dry:
        ratio = dry.get("change_ratio")
        pct = f"{ratio * 100:.1f}%" if ratio is not None else "n/a"
        lines += [
            "",
            "切 enforce 演练：",
            f"  会改动正文的稿件: {dry.get('documents_would_change', 0)}/{dry.get('documents_total', 0)} ({pct})",
            "  说明：只影响含 block 的稿件；pending 永不改文本",
        ]

    if report["errors"]:
        lines += ["", f"回放异常 {len(report['errors'])} 条（前 5）："]
        lines += [f"  - {e}" for e in report["errors"][:5]]
    lines.append("=" * 62)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="历史稿件 shadow 回放（task 5.2）")
    parser.add_argument("--limit", type=int, default=None, help="只回放前 N 篇")
    parser.add_argument(
        "--mode",
        choices=("corpus", "synthetic"),
        default="corpus",
        help="corpus=用语料自带引用线索；synthetic=自动插合成引用标记压测三级",
    )
    parser.add_argument("--manuscripts", type=Path, default=None, help="自定义稿件 JSON")
    parser.add_argument("-o", "--out", type=Path, default=None, help="报告输出路径（默认 stdout）")
    parser.add_argument(
        "--online",
        action="store_true",
        help="允许访问真实元数据上游（默认离线，避免把网络状况混入统计）",
    )
    args = parser.parse_args(argv)

    if args.manuscripts:
        manuscripts = json.loads(args.manuscripts.read_text(encoding="utf-8"))
        if not isinstance(manuscripts, list):
            raise SystemExit("--manuscripts 需为 JSON 数组")
    else:
        manuscripts = load_gov_corpus(args.limit)

    resolver = None if args.online else OfflineResolver()
    report = replay(manuscripts, mode=args.mode, resolver=resolver)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(summarize(report))
        print(f"\n完整报告已写入：{args.out}")
    else:
        print(summarize(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
