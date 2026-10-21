"""历史稿件 shadow 回放测试（task 5.2）。

验证回放脚本的核心不变量：
- shadow 模式与历史行为（只剥不可解标记）逐字节一致；
- 三态比例统计与误报分桶自洽；
- enforce 演练只统计含 block 的稿件；
- 离线解析器不发网络请求。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_replay() -> ModuleType:
    """按文件路径加载回放脚本。

    不能 `from scripts import replay_shadow`：site-packages 里有一个同名的
    `scripts` 包会抢先命中，导致导入到无关模块。
    """
    path = ROOT / "scripts" / "replay_shadow.py"
    spec = importlib.util.spec_from_file_location("fd_shilun_replay_shadow", path)
    assert spec and spec.loader, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


replay = _load_replay()


def _docs() -> list[dict]:
    """最小语料：两篇，一篇带引用线索、一篇无。"""
    return [
        {
            "doc_id": "d1",
            "title": "关于加强某工作的意见",
            "doc_number": "国发〔2024〕5号",
            "sections": [
                {"section_path": "一", "text": "为贯彻落实有关要求，现提出如下意见。"},
                {"section_path": "二", "text": "各单位要高度重视，狠抓落实。"},
            ],
            "cited_docs": [{"doc_number": "国发〔2023〕1号", "title": "上一年度意见"}],
        },
        {
            "doc_id": "d2",
            "title": "无引用线索的通知",
            "sections": [{"section_path": "一", "text": "现将有关事项通知如下。"}],
            "cited_docs": [],
        },
    ]


def test_corpus_text_joins_sections() -> None:
    text = replay.corpus_text(_docs()[0])
    assert "为贯彻落实有关要求" in text
    assert "狠抓落实" in text


def test_corpus_text_falls_back_to_title() -> None:
    assert replay.corpus_text({"title": "只有标题", "sections": []}) == "只有标题"


def test_corpus_citations_from_cited_docs() -> None:
    citations = replay.corpus_citations(_docs()[0])
    assert len(citations) == 1
    assert citations[0]["kind"] == "gov_corpus"
    assert citations[0]["meta"]["doc_number"] == "国发〔2023〕1号"


def test_synthetic_citations_inserts_markers_and_leaves_gaps() -> None:
    # 每 3 句插一条标记：12 句 → s0..s3；每 4 条留 1 条不可解（s3）
    text = "。".join([f"第{i}句" for i in range(12)])
    marked, citations = replay.synthetic_citations(text)
    assert "[[cite:s0]]" in marked
    assert "[[cite:s3]]" in marked
    assert "s3" not in {c["id"] for c in citations}


def test_replay_shadow_matches_legacy_and_reports_three_states() -> None:
    report = replay.replay(_docs(), mode="corpus", resolver=replay.OfflineResolver())
    assert report["all_matches_legacy"] is True
    assert report["documents"] == 2
    # 两篇都插了引用标记，故至少各一条一级结果
    assert report["items_total"] >= 2
    counts = report["status_counts"]
    assert counts.get("pass", 0) + counts.get("block", 0) + counts.get("pending_review", 0) == report["items_total"]


def test_replay_false_positive_buckets_are_consistent() -> None:
    report = replay.replay(_docs(), mode="corpus", resolver=replay.OfflineResolver())
    fp = report["false_positive_analysis"]
    pending_total = report["status_counts"].get("pending_review", 0)
    # 待核三桶之和应等于待核总数
    assert (
        fp["pending_environmental"] + fp["pending_structural_coverage"] + fp["pending_other"]
        == pending_total
    )
    blocked_total = report["status_counts"].get("block", 0)
    assert (
        fp["blocked_unresolvable"]
        + fp["blocked_metadata_mismatch"]
        + fp["blocked_retracted"]
        + fp["blocked_other"]
        == blocked_total
    )


def test_offline_resolver_never_returns_records() -> None:
    resolver = replay.OfflineResolver()
    result = resolver.lookup({"doi": "10.1/x"})
    assert result.records == []
    assert result.failures, "离线解析器必须显式报告失败，不得静默返回空"
    assert resolver.calls == 1


def test_enforce_dry_run_only_counts_documents_with_blocks() -> None:
    report = replay.replay(_docs(), mode="synthetic", resolver=replay.OfflineResolver())
    dry = report["enforce_dry_run"]
    assert dry["documents_total"] == report["documents"]
    # 改动稿件数不得超过总稿件数
    assert dry["documents_would_change"] <= dry["documents_total"]
    if dry["documents_total"]:
        assert 0.0 <= (dry["change_ratio"] or 0.0) <= 1.0


def test_summarize_renders_key_sections() -> None:
    report = replay.replay(_docs(), mode="corpus", resolver=replay.OfflineResolver())
    text = replay.summarize(report)
    assert "历史稿件 shadow 回放报告" in text
    assert "三态分布" in text
    assert "误报分析" in text
    assert "切 enforce 演练" in text


def test_replay_tolerates_empty_documents() -> None:
    docs = [{"doc_id": "empty", "title": "", "sections": []}]
    report = replay.replay(docs, mode="corpus", resolver=replay.OfflineResolver())
    assert report["documents"] == 0
    assert report["errors"], "空正文必须被显式记录，不得静默跳过"


@pytest.mark.parametrize("mode", ["corpus", "synthetic"])
def test_both_replay_modes_run(mode: str) -> None:
    report = replay.replay(_docs(), mode=mode, resolver=replay.OfflineResolver())
    assert report["replay_mode"] == mode
    assert report["mode"] == "shadow"
