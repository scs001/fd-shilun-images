"""三级引用核验测试（tasks 1.2–1.5）：全离线、确定性替身。

不发起任何网络请求：二级核验一律注入 FakeResolver；唯一的 HTTP 解析器
测试走 FD_SHILUN_VERIFY_OFFLINE=1。模式不变量在此锁定——off 与 shadow 的
cleaned_text 必须逐字节一致，且与 writing.resolve_citations 的历史输出相同。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fd_shilun_api import writing
from fd_shilun_api.locator import citation_locator
from fd_shilun_api.verify.authentic import (
    HttpMetadataResolver,
    LookupResult,
    MetadataRecord,
    verify_authentic,
)
from fd_shilun_api.verify.models import (
    RULE_EXISTS,
    RULE_FULLTEXT_MISSING,
    RULE_METADATA,
    RULE_RESOLVABLE,
    RULE_RETRACTED,
    RULE_SUPPORT,
    RULE_UPSTREAM,
)
from fd_shilun_api.verify.resolvable import verify_resolvable
from fd_shilun_api.verify.service import verification_mode, verify_text
from fd_shilun_api.verify.support import verify_support


class FakeResolver:
    """实现 MetadataResolver 协议的确定性替身（可按 meta 分派不同答复）。"""

    def __init__(self, result: LookupResult | object):
        self.result = result
        self.calls: list[dict] = []

    def lookup(self, meta: dict) -> LookupResult:
        self.calls.append(dict(meta))
        if callable(self.result):
            return self.result(meta)
        return self.result  # type: ignore[return-value]


META = {
    "type": "journal-article",
    "title": "Retrieval-Augmented Generation for Knowledge-Intensive Tasks",
    "authors": ["Zhihong Shen", "Beining Cui"],
    "venue": "Expert Systems",
    "year": 2022,
    "volume": "39",
    "issue": "1",
    "pages": "e12770",
    "doi": "10.1111/exsy.12770",
}


def _record(**overrides) -> MetadataRecord:
    base = {
        "source": "crossref",
        # 与 META 只差大小写、连字符与空白：归一化后应视为同题。
        "title": "retrieval augmented generation for knowledge intensive tasks",
        "authors": ["Zhihong Shen"],
        "year": 2022,
        "volume": "39",
        "issue": "1",
        "pages": "e12770",
        "doi": "10.1111/exsy.12770",
    }
    base.update(overrides)
    return MetadataRecord(**base)


def _lookup(**kwargs) -> LookupResult:
    base = {"records": [_record()], "responded": ["crossref"], "failures": []}
    base.update(kwargs)
    return LookupResult(**base)


# ---- 一级：可解 ----


def test_level1_unresolvable_blocks_and_resolvable_passes() -> None:
    text = "甲[[cite:c1]]乙[[cite:ghost]]丙[[cite:c1]]"
    items = verify_resolvable(text, {"c1"})

    # 同一 id 只出一条（取首个出现位置），顺序按首次出现。
    assert [i.locator.citation_id for i in items] == ["c1", "ghost"]
    assert [i.status for i in items] == ["pass", "block"]
    assert all(i.rule_id == RULE_RESOLVABLE and i.level == "resolvable" for i in items)

    start = text.index("[[cite:c1]]")
    first = items[0]
    assert first.locator.char_start == start
    assert first.locator.char_end == start + len("[[cite:c1]]")
    assert first.locator.marker == "[[cite:c1]]"
    assert first.evidence[0].source == "collection"
    assert "不可解" in items[1].reason


def test_level1_empty_collection_blocks_everything() -> None:
    items = verify_resolvable("只有标记 [[cite:x]]", set())
    assert len(items) == 1 and items[0].status == "block"


# ---- 二级：真实 ----


def test_level2_metadata_mismatch_blocks_and_lists_fields() -> None:
    resolver = FakeResolver(_lookup(records=[_record(year=2021, volume="40")]))
    item = verify_authentic(META, resolver, citation_id="c1")[0]

    assert item.status == "block"
    assert item.rule_id == RULE_METADATA
    fields = item.evidence[0].fields
    assert set(fields) == {"year", "volume"}
    assert fields["year"] == {"expected": 2022, "found": 2021}
    assert fields["volume"] == {"expected": "39", "found": "40"}
    # 标题只差大小写/标点，不应进入不一致字段。
    assert "title" not in fields


def test_level2_missing_fields_never_invent_mismatch() -> None:
    # 记录只给出题名/作者/年份：卷期页缺席不等于不符。
    sparse = MetadataRecord(
        source="openalex",
        title="Retrieval augmented generation for knowledge intensive tasks",
        authors=["Zhihong Shen"],
        year="2022",  # type: ignore[arg-type]
    )
    item = verify_authentic(
        META, FakeResolver(_lookup(records=[sparse], responded=["openalex"])), citation_id="c1"
    )[0]
    assert item.status == "pass"
    matched = item.evidence[0].fields["matched_fields"]
    assert {"title", "first_author", "year"} <= set(matched)
    assert "volume" not in matched and "pages" not in matched


def test_level2_consistent_record_passes_with_source() -> None:
    item = verify_authentic(META, FakeResolver(_lookup()), citation_id="c1")[0]
    assert item.status == "pass"
    assert item.rule_id == RULE_METADATA
    assert item.evidence[0].source == "crossref"
    assert item.evidence[0].fields["matched_fields"] == [
        "title",
        "first_author",
        "year",
        "volume",
        "issue",
        "pages",
        "doi",
    ]


def test_level2_retracted_blocks() -> None:
    resolver = FakeResolver(_lookup(records=[_record(retracted=True)]))
    item = verify_authentic(META, resolver, citation_id="c1")[0]
    assert item.status == "block"
    assert item.rule_id == RULE_RETRACTED
    assert "撤稿" in item.reason
    assert item.evidence[0].fields["retracted"] is True


def test_level2_all_channels_failed_is_pending() -> None:
    resolver = FakeResolver(
        LookupResult(records=[], responded=[], failures=["crossref: timeout"])
    )
    item = verify_authentic(META, resolver, citation_id="c1")[0]
    assert item.status == "pending_review"
    assert item.rule_id == RULE_UPSTREAM
    assert "crossref: timeout" in item.reason
    assert item.evidence[0].fields["failures"] == ["crossref: timeout"]


def test_level2_no_record_but_responded_is_block_exists() -> None:
    resolver = FakeResolver(LookupResult(records=[], responded=["crossref", "openalex"]))
    item = verify_authentic(META, resolver, citation_id="c1")[0]
    assert item.status == "block"
    assert item.rule_id == RULE_EXISTS
    assert item.reason == "来源不存在"
    assert {e.fields["channel"] for e in item.evidence} == {"crossref", "openalex"}


def test_http_resolver_offline_makes_no_network_calls(monkeypatch) -> None:
    monkeypatch.setenv("FD_SHILUN_VERIFY_OFFLINE", "1")
    result = HttpMetadataResolver(timeout=0.01).lookup(META)
    assert result.records == []
    assert result.responded == []
    assert result.failures == []

    # 离线 → 无答复、无失败 → 只能 pending（上游不可用），绝不是「来源不存在」。
    item = verify_authentic(META, HttpMetadataResolver(), citation_id="c1")[0]
    assert item.status == "pending_review" and item.rule_id == RULE_UPSTREAM


def test_http_resolver_parses_all_channels_against_local_server(monkeypatch) -> None:
    """四个通道的 URL 覆盖与响应解析：本地回环服务，不触外网。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 约定
            path = urlparse(self.path).path
            if path.startswith("/crossref/works"):
                payload = {
                    "message": {
                        "title": [
                            "Retrieval-Augmented Generation for Knowledge-Intensive Tasks"
                        ],
                        "author": [{"given": "Zhihong", "family": "Shen"}],
                        "issued": {"date-parts": [[2022]]},
                        "volume": "39",
                        "issue": "1",
                        "page": "e12770",
                        "DOI": "10.1111/exsy.12770",
                    }
                }
            elif path.startswith("/openalex/works"):
                payload = {
                    "results": [
                        {
                            "display_name": (
                                "Retrieval-Augmented Generation for "
                                "Knowledge-Intensive Tasks"
                            ),
                            "authorships": [
                                {"author": {"display_name": "Zhihong Shen"}}
                            ],
                            "publication_year": 2022,
                            "biblio": {
                                "volume": "39",
                                "issue": "1",
                                "first_page": "e12770",
                            },
                            "doi": "https://doi.org/10.1111/exsy.12770",
                            "is_retracted": False,
                        }
                    ]
                }
            elif path.startswith("/datacite/dois"):
                payload = {
                    "data": {
                        "attributes": {
                            "titles": [
                                {
                                    "title": (
                                        "Retrieval-Augmented Generation for "
                                        "Knowledge-Intensive Tasks"
                                    )
                                }
                            ],
                            "creators": [{"name": "Zhihong Shen"}],
                            "publicationYear": 2022,
                            "volume": "39",
                            "issue": "1",
                            "page": "e12770",
                            "doi": "10.1111/exsy.12770",
                        }
                    }
                }
            elif path.endswith("/esearch.fcgi"):
                payload = {"esearchresult": {"idlist": ["123"]}}
            elif path.endswith("/esummary.fcgi"):
                payload = {
                    "result": {
                        "uids": ["123"],
                        "123": {
                            "uid": "123",
                            "title": (
                                "Retrieval-Augmented Generation for "
                                "Knowledge-Intensive Tasks."
                            ),
                            "authors": [{"name": "Zhihong Shen"}],
                            "pubdate": "2022",
                            "volume": "39",
                            "issue": "1",
                            "pages": "e12770",
                            "articleids": [
                                {"idtype": "doi", "value": "10.1111/exsy.12770"}
                            ],
                            "pubtype": ["Journal Article"],
                        },
                    }
                }
            else:
                payload = {}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.delenv("FD_SHILUN_VERIFY_OFFLINE", raising=False)
    # 本机装有系统代理，回环地址必须显式绕过。
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("FD_SHILUN_CROSSREF_URL", f"{base}/crossref/works")
    monkeypatch.setenv("FD_SHILUN_OPENALEX_URL", f"{base}/openalex/works")
    monkeypatch.setenv("FD_SHILUN_DATACITE_URL", f"{base}/datacite/dois")
    monkeypatch.setenv("FD_SHILUN_PUBMED_URL", f"{base}/pubmed")
    try:
        resolver = HttpMetadataResolver(timeout=5.0)
        result = resolver.lookup(META)
        assert result.responded == ["crossref", "openalex", "datacite", "pubmed"]
        assert result.failures == []
        assert {r.source for r in result.records} == {
            "crossref",
            "openalex",
            "datacite",
            "pubmed",
        }
        assert verify_authentic(META, resolver, citation_id="c1")[0].status == "pass"
    finally:
        server.shutdown()
        server.server_close()


# ---- 三级：支持 ----


def test_level3_supported_assertion_passes() -> None:
    assertion = "检索增强生成在知识密集型任务上显著提升了准确性"
    fulltext = "实验表明，检索增强生成在知识密集型任务上显著提升了准确性。其他段落与此无关。"
    item = verify_support(
        assertion, fulltext, citation_id="c1", locator=citation_locator("c1")
    )
    assert item.status == "pass", item.reason
    assert item.rule_id == RULE_SUPPORT
    fields = item.evidence[0].fields
    assert fields["score"] >= fields["threshold"]
    assert "检索增强生成" in fields["closest_sentence"]


def test_level3_latin_assertion_uses_token_similarity() -> None:
    assertion = "retrieval augmented generation improves question answering accuracy"
    fulltext = (
        "We show that retrieval augmented generation improves question answering "
        "accuracy on open-domain benchmarks."
    )
    item = verify_support(assertion, fulltext, citation_id="c1")
    assert item.status == "pass", item.reason
    assert item.evidence[0].fields["score"] >= 0.34


def test_level3_unsupported_assertion_blocks() -> None:
    assertion = "深度学习使诊断准确率提升了 3.7 个百分点"
    fulltext = (
        "表 2 列出了 3.7 的系数。鱼类栖息地的季节性变化与水温相关，本研究未涉及医学诊断。"
    )
    item = verify_support(assertion, fulltext, citation_id="c1")
    assert item.status == "block", item.reason
    assert item.rule_id == RULE_SUPPORT
    fields = item.evidence[0].fields
    assert fields["score"] < fields["threshold"]
    assert fields["missing_numbers"] == []  # 数字虽然命中，仍不算支持


def test_level3_missing_number_blocks() -> None:
    assertion = "该方法将准确率提升到 97.5%"
    fulltext = "该方法将准确率提升到 87.5%。"
    item = verify_support(assertion, fulltext, citation_id="c1")
    assert item.status == "block"
    assert item.evidence[0].fields["missing_numbers"] == ["97.5"]


def test_level3_number_normalization_matches_thousands_separator() -> None:
    assertion = "样本量为 1,000 例"
    fulltext = "本研究样本量为 1000 例。"
    item = verify_support(assertion, fulltext, citation_id="c1")
    assert item.status == "pass", item.reason


def test_level3_no_fulltext_is_pending() -> None:
    for empty in (None, "", "   "):
        item = verify_support("任意断言", empty, citation_id="c1")
        assert item.status == "pending_review"
        assert item.rule_id == RULE_FULLTEXT_MISSING
        assert "不计入已通过" in item.reason


# ---- 模式与文本处置（task 1.5 的字节不变量） ----


def _citations() -> list[dict]:
    return [
        {"id": "c1", "kind": "openalex", "ref_id": "W1", "meta": dict(META)},
        {
            "id": "c2",
            "kind": "openalex",
            "ref_id": "W2",
            "meta": {"title": "Another paper", "doi": "10.9999/other", "year": 2020},
        },
    ]


def _mode_resolver(meta: dict) -> LookupResult:
    # c1：元数据不符 → block；c2：上游失联 → pending。
    if meta.get("doi") == META["doi"]:
        return _lookup(records=[_record(year=2021)])
    return LookupResult(failures=["openalex: timeout"])


def test_off_and_shadow_are_byte_identical_and_match_legacy() -> None:
    text = "甲句 [[cite:c1]]。乙句 [[cite:c2]]。丙句 [[cite:ghost]]。"
    resolver = FakeResolver(_mode_resolver)
    off = verify_text(text, _citations(), mode="off", resolver=resolver)
    shadow = verify_text(text, _citations(), mode="shadow", resolver=resolver)

    legacy = writing.resolve_citations(text, {"c1", "c2"})["cleaned_text"]
    assert off.cleaned_text == shadow.cleaned_text == legacy
    # 报告在 off 下仍然产出（mode 归入 shadow，真实开关另行返回）。
    assert off.report.counts.total > 0
    assert off.report.mode == "shadow"


def test_enforce_strips_blocked_keeps_pending_and_other_text() -> None:
    text = "甲句 [[cite:c1]]。乙句 [[cite:c2]]。丙句 [[cite:ghost]]。"
    out = verify_text(
        text, _citations(), mode="enforce", resolver=FakeResolver(_mode_resolver)
    )

    # c1 被 block → 剥除；ghost 不可解 → 剥除；c2 pending → 保留。
    assert out.cleaned_text == "甲句 。乙句 [[cite:c2]]。丙句 。"
    assert set(out.report.blocked_citation_ids) == {"ghost", "c1"}
    assert out.report.pending_citation_ids == ["c2"]
    assert [a.citation_id for a in out.annotations] == ["c2"]
    assert "上游不可用" in out.annotations[0].reason


def test_pending_never_changes_text_in_shadow() -> None:
    text = "只有一句 [[cite:c2]]。"
    out = verify_text(
        text, _citations(), mode="shadow", resolver=FakeResolver(_mode_resolver)
    )
    assert out.cleaned_text == text


def test_verification_mode_env_parsing(monkeypatch) -> None:
    monkeypatch.delenv("FD_SHILUN_CITE_VERIFY_MODE", raising=False)
    assert verification_mode() == "shadow"
    monkeypatch.setenv("FD_SHILUN_CITE_VERIFY_MODE", "enforce")
    assert verification_mode() == "enforce"
    monkeypatch.setenv("FD_SHILUN_CITE_VERIFY_MODE", "OFF")
    assert verification_mode() == "off"
    monkeypatch.setenv("FD_SHILUN_CITE_VERIFY_MODE", "bogus")
    assert verification_mode() == "shadow"
    # 显式传入未知模式同样回落 shadow，避免拼写错误导致闸门意外全开。
    out = verify_text("x", [], mode="bogus", resolver=FakeResolver(LookupResult()))
    assert out.report.mode == "shadow"


# ---- 服务编排：全文来源与抽象摘要隔离 ----


def test_service_never_treats_abstract_as_fulltext() -> None:
    assertion = "检索增强生成在知识密集型任务上显著提升了准确性"
    citations = [
        {
            "id": "c1",
            "kind": "openalex",
            "ref_id": "W1",
            "meta": {**META, "abstract": assertion + "。"},
        }
    ]
    out = verify_text(
        f"{assertion} [[cite:c1]]。",
        citations,
        mode="shadow",
        resolver=FakeResolver(_lookup()),
    )
    support = [i for i in out.report.items if i.level == "support"]
    assert len(support) == 1
    assert support[0].status == "pending_review"
    assert support[0].rule_id == RULE_FULLTEXT_MISSING


def test_service_uses_explicit_fulltext_param_and_meta_fulltext() -> None:
    assertion = "检索增强生成在知识密集型任务上显著提升了准确性"
    text = f"{assertion} [[cite:c1]]。"
    citations = [{"id": "c1", "kind": "openalex", "ref_id": "W1", "meta": dict(META)}]

    via_param = verify_text(
        text,
        citations,
        resolver=FakeResolver(_lookup()),
        fulltexts={"c1": assertion + "。"},
    )
    assert [i for i in via_param.report.items if i.level == "support"][0].status == "pass"

    citations[0]["meta"] = {**META, "fulltext": assertion + "。"}
    via_meta = verify_text(text, citations, resolver=FakeResolver(_lookup()))
    assert [i for i in via_meta.report.items if i.level == "support"][0].status == "pass"


def test_service_marks_uncovered_source_kind_pending() -> None:
    citations = [
        {"id": "g1", "kind": "gov_corpus", "ref_id": "gov-1", "meta": {"title": "某通知"}}
    ]
    out = verify_text(
        "依据 [[cite:g1]]。",
        citations,
        mode="enforce",
        resolver=FakeResolver(_lookup()),
    )
    authentic = [i for i in out.report.items if i.level == "authentic"]
    assert authentic and authentic[0].status == "pending_review"
    assert authentic[0].rule_id == RULE_UPSTREAM
    # pending 不改文本：enforce 下这个标记仍在。
    assert "[[cite:g1]]" in out.cleaned_text


def test_service_level1_block_skips_levels_two_and_three() -> None:
    out = verify_text(
        "伪引用 [[cite:ghost]]。",
        _citations(),
        resolver=FakeResolver(_lookup()),
    )
    assert [i.level for i in out.report.items] == ["resolvable"]
    assert out.report.blocked_citation_ids == ["ghost"]


# ---- writing.validate 集成（task 1.5） ----


def test_validate_exposes_verification_report_and_keeps_text(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FD_SHILUN_DB", str(tmp_path / "verify.db"))
    monkeypatch.setenv("FD_SHILUN_VERIFY_OFFLINE", "1")
    monkeypatch.delenv("FD_SHILUN_CITE_VERIFY_MODE", raising=False)
    monkeypatch.delenv("FD_SHILUN_LLM_API_KEY", raising=False)

    app = FastAPI()
    app.include_router(writing.router)
    app.dependency_overrides[writing.require_user] = lambda: "user_a"
    client = TestClient(app)

    proj = client.post(
        "/api/v1/writing/projects", json={"title": "核验稿", "kind": "thesis"}
    ).json()
    cid = client.post(
        f"/api/v1/writing/projects/{proj['id']}/citations",
        json={"kind": "openalex", "ref_id": "W1", "meta": META},
    ).json()["id"]

    text = f"正文断言 [[cite:{cid}]] 与伪引用 [[cite:ghost]]。"
    out = client.post(
        f"/api/v1/writing/projects/{proj['id']}/validate", json={"text": text}
    ).json()

    assert out["verification_mode"] == "shadow"
    assert out["verification"]["report_version"] == "cite-verify-v1"
    assert out["verification"]["blocked_citation_ids"] == ["ghost"]
    assert out["cleaned_text"] == f"正文断言 [[cite:{cid}]] 与伪引用 。"
    # 离线 + 无全文：真实来源 c1 只能待核，且绝不改文本。
    assert [a["citation_id"] for a in out["annotations"]] == [cid]
    assert "上游不可用" in out["annotations"][0]["reason"]
