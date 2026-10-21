"""核验报告契约测试（task 1.6）：报告过 schema 校验，locator 真的索引原文。

前端高亮依赖两件事，缺一就只是装饰：
1. 报告本身是合法的 `VerificationReport`（可机读、字段齐备）；
2. 每条带字符区间的 locator，切片 `text[char_start:char_end]` 必须等于它声明的
   marker（或落在该引用的标记内）——偏移错了，界面就会高亮到别的字。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fd_shilun_api import writing
from fd_shilun_api.verify.authentic import LookupResult, MetadataRecord
from fd_shilun_api.verify.models import VerificationReport


class _ConsistentResolver:
    """确定性替身：所有查询都返回一条与引用元数据完全一致的记录。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def lookup(self, meta: dict) -> LookupResult:
        self.calls.append(meta)
        return LookupResult(
            records=[
                MetadataRecord(
                    source="crossref",
                    title=str(meta.get("title") or ""),
                    authors=meta.get("authors") or [],
                    year=meta.get("year"),
                    volume=meta.get("volume"),
                    issue=meta.get("issue"),
                    pages=meta.get("pages"),
                    doi=meta.get("doi"),
                )
            ],
            responded=["crossref"],
        )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_SHILUN_DB", str(tmp_path / "report.db"))
    monkeypatch.delenv("FD_SHILUN_LLM_API_KEY", raising=False)
    monkeypatch.setenv("FD_SHILUN_VERIFY_OFFLINE", "1")
    monkeypatch.delenv("FD_SHILUN_CITE_VERIFY_MODE", raising=False)
    monkeypatch.setattr(writing, "metadata_resolver", _ConsistentResolver())
    app = FastAPI()
    app.include_router(writing.router)
    app.dependency_overrides[writing.require_user] = lambda: "u1"
    return TestClient(app)


def _setup(tc: TestClient) -> tuple[str, str, str]:
    pid = tc.post("/api/v1/writing/projects", json={"title": "稿", "kind": "thesis"}).json()["id"]
    good = tc.post(
        f"/api/v1/writing/projects/{pid}/citations",
        json={
            "kind": "openalex",
            "ref_id": "W1",
            "meta": {"title": "On Evidence", "authors": ["Ada Lovelace"], "year": 2020},
        },
    ).json()["id"]
    return pid, good, "deadbeefcafe"


def test_report_schema_and_locators_index_text(client) -> None:
    pid, good, bad = _setup(client)
    text = (
        "第一段：结论有依据[[cite:%s]]。\n"
        "第二段：这里引用了不存在的条目[[cite:%s]]。"
    ) % (good, bad)

    data = client.post(f"/api/v1/writing/projects/{pid}/validate", json={"text": text}).json()

    # 1) 回显原文，且报告是合法 schema（pydantic 校验即 schema 校验）
    assert data["text"] == text
    report = VerificationReport.model_validate(data["verification"])
    assert data["verification_mode"] in ("off", "shadow", "enforce")
    assert report.mode == "shadow"  # off/shadow 都归 shadow，真实开关在 verification_mode

    # 2) 计数与条目自洽；pending 不计入通过
    assert report.counts.total == len(report.items)
    assert report.counts.passed + report.counts.blocked + report.counts.pending == len(report.items)

    # 3) locator 逐条落到原文的真实位置上
    ranged = [i for i in report.items if i.locator.char_start is not None]
    assert ranged, "报告里没有任何带字符区间的条目，前端无法定位"
    for item in ranged:
        seg = data["text"][item.locator.char_start : item.locator.char_end]
        assert seg, f"{item.rule_id} 的区间切出了空串"
        assert item.locator.citation_id in seg, (
            f"{item.rule_id} 的 locator 未指向该引用的标记：{seg!r}"
        )
        assert item.locator.marker == seg  # 声明的 marker 与实际切片一致

    # 4) 不可解引用被明确拦截，可解引用至少没有 block
    assert bad in report.blocked_citation_ids
    assert good not in report.blocked_citation_ids
    assert any(i.locator.citation_id == bad and i.status == "block" for i in report.items)
    assert not any(
        i.locator.citation_id == good and i.level == "resolvable" and i.status == "block"
        for i in report.items
    )

    # 5) 每条判定都带非空理由，报告方可审计
    assert all(i.reason.strip() for i in report.items if i.status != "pass")


def test_pending_is_reported_not_counted_as_pass(client) -> None:
    """无全文的引用：标待核、保留在稿中、不计入 pass（规范：不得静默放行）。"""
    pid, good, _ = _setup(client)
    text = "结论有依据[[cite:%s]]。" % good

    data = client.post(f"/api/v1/writing/projects/{pid}/validate", json={"text": text}).json()
    report = VerificationReport.model_validate(data["verification"])

    support = [i for i in report.items if i.level == "support" and i.locator.citation_id == good]
    assert len(support) == 1
    assert support[0].status == "pending_review"
    assert support[0].rule_id == "cite.fulltext_missing"
    # 待核保留在稿中：标记没被剥掉
    assert f"[[cite:{good}]]" in data["cleaned_text"]
    # 标注带原因
    assert any(a["citation_id"] == good and a["reason"] for a in data["annotations"])
    assert report.counts.pending >= 1
