"""核验结果模型 schema 单测（task 1.1）：三态序列化/反序列化与汇总口径。

离线纯模型测试，不打网络、不起服务。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from fd_shilun_api.locator import Locator, citation_locator, field_locator
from fd_shilun_api.verify.models import (
    Evidence,
    VerificationItem,
    VerificationReport,
    build_report,
)


def _item(status: str, *, cid: str = "c1", level: str = "authentic") -> VerificationItem:
    return VerificationItem(
        rule_id="cite.metadata",
        level=level,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        locator=citation_locator(cid, char_start=5, char_end=20, marker="[[cite:c1]]"),
        evidence=[Evidence(source="crossref", detail="作者/年份一致", fields={"year": 2020})],
        reason=f"判定为 {status}",
    )


@pytest.mark.parametrize("status", ["pass", "block", "pending_review"])
def test_three_states_roundtrip(status: str) -> None:
    item = _item(status)
    payload = item.model_dump_json()
    back = VerificationItem.model_validate_json(payload)
    assert back == item
    assert back.status == status
    assert back.locator.citation_id == "c1"
    assert back.evidence[0].source == "crossref"
    # 反序列化后仍是合法 JSON 且字段齐备
    raw = json.loads(payload)
    assert {"rule_id", "level", "status", "locator", "evidence", "reason"} <= set(raw)


def test_report_roundtrip_and_counts() -> None:
    items = [
        _item("pass", cid="a"),
        _item("block", cid="b"),
        _item("pending_review", cid="c"),
        _item("pending_review", cid="c"),
    ]
    report = build_report(items, mode="shadow", citation_count=3)
    # pending 不计入已通过
    assert report.counts.total == 4
    assert report.counts.passed == 1
    assert report.counts.blocked == 1
    assert report.counts.pending == 2
    assert report.blocked_citation_ids == ["b"]
    assert report.pending_citation_ids == ["c"]
    assert report.blocking is True

    back = VerificationReport.model_validate_json(report.model_dump_json())
    assert back == report
    assert back.counts.pass_rate == 0.25


def test_unknown_status_rejected() -> None:
    with pytest.raises(ValidationError):
        _item("accepted")  # 三态之外一律拒绝


def test_locator_range_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        Locator(kind="char_range", char_start=30, char_end=10)
    # 字段锚点可用于公文要素这类无字符区间的问题
    assert field_locator("发文字号").kind == "field"


def test_empty_report_has_no_pass_rate() -> None:
    report = build_report([], mode="enforce", citation_count=0)
    assert report.counts.pass_rate is None
    assert report.blocking is False
