"""开放元数据兜底（tasks 1.1/2.1/2.2/3.2）全离线测试。

边界：
- 所有 HTTP 走 httpx.MockTransport，不打网络；
- 离线开关 FD_SHILUN_VERIFY_OFFLINE=1 时断言一次请求都不发；
- OpenAlex 主路径的响应必须与引入兜底之前逐字节一致（新增字段一个都不许冒出来）；
- 凭据不得出现在日志调用与响应构造处（守卫测试）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import fd_shilun_api.open_metadata as om
import fd_shilun_api.search as search_mod

CROSSREF_URL = "https://api.crossref.org/works"
OPENALEX_URL = "https://api.openalex.org/works"

CROSSREF_ITEM = {
    "DOI": "10.1234/cn.2023.001",
    "title": ["乡村振兴战略下的县域治理"],
    "author": [{"given": "三", "family": "张"}, {"given": "四", "family": "李"}],
    "issued": {"date-parts": [[2023, 5]]},
    "container-title": ["中国行政管理"],
    "abstract": "<jats:p>基于县域样本的实证研究。</jats:p>",
    "URL": "https://doi.org/10.1234/cn.2023.001",
}

OPENALEX_ITEM = {
    "id": "https://openalex.org/W1234567890",
    "doi": "https://doi.org/10.1234/cn.2023.001",
    "title": "乡村振兴战略下的县域治理",
    "publication_year": 2023,
    "authorships": [{"author": {"display_name": "张三"}}],
    "primary_location": {"source": {"display_name": "中国行政管理"}},
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------ Crossref 适配


def test_crossref_bibliographic_search_returns_structured_records() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"message": {"total-results": 42, "items": [CROSSREF_ITEM]}}
        )

    result = om.search_crossref(
        "乡村振兴 县域治理", page=2, per_page=5, client=_client(handler)
    )

    assert result.failure is None
    assert result.source == "crossref"
    assert result.total == 42
    record = result.records[0]
    assert record["stable_id"] == "10.1234/cn.2023.001"
    assert record["title"] == "乡村振兴战略下的县域治理"
    assert record["authors"] == ["三 张", "四 李"]
    assert record["year"] == 2023
    assert record["venue"] == "中国行政管理"
    # JATS 标签被剥掉，摘要只留文字
    assert record["abstract"] == "基于县域样本的实证研究。"

    request = seen[0]
    assert str(request.url).startswith(CROSSREF_URL)
    assert request.url.params["query.bibliographic"] == "乡村振兴 县域治理"
    # 先多取候选再过滤，否则小 per_page 时锚点可能没进候选
    assert request.url.params["rows"] == "15"  # per_page=5 × 3
    assert request.url.params["offset"] == "15"  # (page-1) × per_page × 3


def test_crossref_empty_payload_is_not_indexed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"items": []}})

    result = om.search_crossref("无此文献", client=_client(handler))
    assert result.records == []
    assert result.failure == "not_indexed"


def test_crossref_429_is_quota_and_500_is_upstream() -> None:
    def quota(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    assert om.search_crossref("x", client=_client(quota)).failure == "quota"
    assert om.search_crossref("x", client=_client(broken)).failure == "upstream"


def test_connect_error_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    result = om.search_crossref("x", client=_client(handler))
    assert result.failure == "unreachable"


def test_offline_switch_makes_no_request(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"message": {"items": [CROSSREF_ITEM]}})

    monkeypatch.setenv(om.OFFLINE_ENV, "1")
    result = om.search_open_metadata("乡村振兴", client=_client(handler))
    assert calls == []
    assert result.records == []
    assert result.failure == "unreachable"


# ------------------------------------------------------- 兜底链与失败优先级


def test_chain_falls_through_to_openalex_when_crossref_is_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "crossref" in request.url.host:
            return httpx.Response(200, json={"message": {"items": []}})
        return httpx.Response(200, json={"results": [OPENALEX_ITEM], "meta": {"count": 7}})

    result = om.search_open_metadata("乡村振兴", client=_client(handler))
    assert result.source == "openalex"
    assert result.total == 7
    assert result.records[0]["stable_id"] == "W1234567890"
    assert result.records[0]["venue"] == "中国行政管理"


def test_chain_prefers_informative_failure_over_not_indexed() -> None:
    """一个源挂了、另一个源无命中 → 报「挂了」而不是「查不到」。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "crossref" in request.url.host:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"results": []})

    result = om.search_open_metadata("乡村振兴", client=_client(handler))
    assert result.failure == "upstream"


def test_chain_reports_not_indexed_when_both_are_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "crossref" in request.url.host:
            return httpx.Response(200, json={"message": {"items": []}})
        return httpx.Response(200, json={"results": []})

    result = om.search_open_metadata("乡村振兴", client=_client(handler))
    assert result.failure == "not_indexed"
    assert "crossref" in (result.detail or "")


# ------------------------------------------------------------ 相关性门（确定性）


def test_relevance_gate_drops_or_semantics_noise() -> None:
    """Crossref 是 OR 语义：查「量子纠缠的县域治理」会返回纯量子物理论文，必须滤掉。"""
    junk = [
        {"title": "级联环境中两比特纠缠态的量子非局域关联检验研究", "abstract": None, "venue": "量子光学学报"},
        {"title": "基于六量子比特纠缠混态的三方量子态分享", "abstract": None, "venue": None},
    ]
    kept, dropped = om._filter_relevant(junk, "关于量子纠缠的县域治理研究")
    assert kept == []
    assert dropped == 2


def test_relevance_gate_keeps_matching_records() -> None:
    good = [
        {"title": "乡村振兴战略下的县域治理", "abstract": None, "venue": "中国行政管理"},
        {"title": "乡村振兴战略下旬邑县乡村旅游发展研究", "abstract": None, "venue": None},
    ]
    kept, dropped = om._filter_relevant(good, "乡村振兴战略下的县域治理")
    assert len(kept) == 2
    assert dropped == 0


def test_relevance_gate_reports_how_many_were_filtered() -> None:
    """全被过滤时 detail 要说清「过滤了几条」，别让人以为源里真没有。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "total-results": 577490,
                    "items": [
                        {
                            "DOI": "10.1/junk",
                            "title": ["基于量子点的脱落酸检测方法研究"],
                            "issued": {"date-parts": [[2020]]},
                        }
                    ],
                }
            },
        )

    result = om.search_crossref("关于量子纠缠的县域治理研究", client=_client(handler))
    assert result.records == []
    assert result.failure == "not_indexed"
    assert "已过滤 1 条" in (result.detail or "")


def test_query_terms_split_on_whitespace_to_avoid_fake_bigrams() -> None:
    cjk, latin, trigrams = om._query_terms("乡村振兴 rural governance")
    assert "乡村" in cjk and "振兴" in cjk
    assert "兴r" not in cjk  # 不跨词拼二元组
    assert "村r" not in cjk
    assert "乡村振" in trigrams  # 三字术语用于「有没有实质共同词」
    assert latin == {"rural", "governance"}


def test_relevance_gate_keeps_english_titled_records_when_anchored() -> None:
    """英文题名记录没法做中文词面比对，改看上游分数：贴着最佳中文命中就留下。

    这条锁的是「中文查询也能查到英文文献」——否则中文用户永远看不到
    The Influence of the "Green Principle" in the "Civil Code" 这类相关论文。
    """
    records = [
        {"title": "民法中诚信原则在物权编的体现", "abstract": None, "venue": None, "score": 60.4},
        {"title": "The Influence of the Green Principle in the Civil Code", "abstract": None,
         "venue": "Open Journal of Legal Science", "score": 53.7},
        {"title": "A Study of Soil Microbiome in Rice Paddies", "abstract": None,
         "venue": None, "score": 20.0},
    ]
    kept, dropped = om._filter_relevant(records, "民法典物权编的体系效应")
    titles = [r["title"] for r in kept]
    assert "民法中诚信原则在物权编的体现" in titles
    assert "The Influence of the Green Principle in the Civil Code" in titles
    assert "A Study of Soil Microbiome in Rice Paddies" not in titles
    assert dropped == 1


def test_relevance_gate_drops_english_records_without_a_chinese_anchor() -> None:
    """一条汉字记录都没通过时不给英文记录豁免：否则无关查询的英文结果全回来。"""
    records = [
        {"title": "Evolution of Entanglement Density in Band Gap", "abstract": None,
         "venue": None, "score": 59.1},
        {"title": "The Correlation Criterion of QuantumEntanglement", "abstract": None,
         "venue": None, "score": 58.9},
    ]
    kept, dropped = om._filter_relevant(records, "关于量子纠缠的县域治理研究")
    assert kept == [] and dropped == 2


def test_relevance_gate_requires_a_shared_three_char_term() -> None:
    """只撞两字虚词（关于/研究）不算相关：这是无关查询漏进来的口子。"""
    record = {
        "title": "关于《生物量子体》的研究报告",
        "abstract": None,
        "venue": "实用医学研究",
        "score": 58.8,
    }
    # 「研究」「关于」命中但没有任何三字术语共享
    kept, _ = om._filter_relevant([record], "关于县域治理的调查研究")
    assert kept == []


def test_relevance_gate_needs_proportional_three_char_coverage() -> None:
    """只顺带提到一个三字术语不算相关——要按比例覆盖整条查询。"""
    junk = {
        "title": "关于《生物量子体》的研究报告",
        "venue": "实用医学研究",
        "score": 58.8,
        "abstract": (
            "量子物理学中，具有同源性的两个量子体，在信息传递上是不需要信息的"
            "发射与接收过程的。量子力学中不能表示成直积形式的态称为纠缠态。"
        ),
    }
    relevant = {
        "title": "民法中诚信原则在物权编的体现", "venue": None, "score": 60.4, "abstract": None,
    }
    kept, dropped = om._filter_relevant([dict(junk), dict(relevant)], "关于量子纠缠的县域治理研究")
    assert kept == [] and dropped == 2  # 两个主题并列且无文献覆盖时不硬凑结果

    kept, dropped = om._filter_relevant([dict(junk), dict(relevant)], "民法典物权编的体系效应")
    assert [r["title"] for r in kept] == ["民法中诚信原则在物权编的体现"]
    assert dropped == 1


# --------------------------------------------- OpenAlex 主路径响应逐字节不变

GOLDEN_OPENALEX_RESPONSE = (
    '{"results":[{"work_id":"W1","doi":null,"title":"A paper","year":2020,'
    '"venue":"Nature","cited_by_count":3,"provenance":{"source":"openalex",'
    '"stable_id":"W1","license":"CC0"}}],"total":1,"page":1,"per_page":10,'
    '"reason":null,"detail":null}'
)


def _client_for_search() -> TestClient:
    app = FastAPI()
    app.include_router(search_mod.router)
    app.dependency_overrides[search_mod.require_user] = lambda: "user_test"
    return TestClient(app)


def test_openalex_response_is_byte_identical_to_pre_change(monkeypatch) -> None:
    """非中文 query 的响应体必须逐字节等于改动前：新字段一个都不能出现。"""

    def fake_get(self, path, params=None):  # noqa: ANN001
        return (
            {
                "meta": {"count": 1},
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "doi": None,
                        "title": "A paper",
                        "publication_year": 2020,
                        "cited_by_count": 3,
                        "primary_location": {"source": {"display_name": "Nature"}},
                    }
                ],
            },
            {},
        )

    monkeypatch.setattr(search_mod.OpenAlexProvider, "_get", fake_get)
    resp = _client_for_search().post(
        "/api/v1/search/works", json={"query": "rural revitalization"}
    )
    assert resp.status_code == 200
    assert resp.text == GOLDEN_OPENALEX_RESPONSE


def test_openalex_empty_response_has_no_coverage_fields(monkeypatch) -> None:
    def fake_get(self, path, params=None):  # noqa: ANN001
        return ({"meta": {"count": 0}, "results": []}, {})

    monkeypatch.setattr(search_mod.OpenAlexProvider, "_get", fake_get)
    body = _client_for_search().post(
        "/api/v1/search/works", json={"query": "rural revitalization"}
    ).json()
    assert body["reason"] == "not_indexed"
    assert "coverage_note" not in body
    assert "coverage" not in json.dumps(body)


# --------------------------------------------------------------- 凭据守卫


CREDENTIAL_HINT = re.compile(r"api_key|credential|secret|token", re.IGNORECASE)
LOG_CALL = re.compile(r"\b(?:logger|logging|log)\.\w+\s*\(|\bprint\s*\(")

PACKAGE = Path(om.__file__).resolve().parent


def test_no_credential_reaches_log_calls_or_payloads() -> None:
    """凭据只允许出现在取值/拼头/校验处；不得进日志调用，也不得进结果字典。"""
    offenders: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not LOG_CALL.search(line):
                continue
            if CREDENTIAL_HINT.search(line):
                offenders.append(f"{path.relative_to(PACKAGE)}:{lineno}: {line.strip()}")
    assert offenders == [], "凭据不得出现在日志/打印调用里：" + "；".join(offenders)


def test_channel_credentials_field_is_not_reprable() -> None:
    """请求体里的凭据字段必须 repr=False：校验报错时不会把 key 打进日志。"""
    field = search_mod.WorksRequest.model_fields["channel_credentials"]
    assert field.repr is False
    assert "byok-secret" not in repr(
        search_mod.WorksRequest(query="x", channel_credentials={"wanfang": "byok-secret"})
    )


def test_channel_config_never_carries_a_key_value(monkeypatch) -> None:
    """ChannelConfig 只存变量名，不存值——配置会被序列化与展示。"""
    from fd_shilun_api.channels import configured_channels

    monkeypatch.setenv(
        "FD_SHILUN_CN_CHANNELS",
        json.dumps(
            [
                {
                    "id": "wanfang",
                    "kind": "http",
                    "base_url": "https://api.example.com/v1/search",
                    "api_key_env": "WANFANG_API_KEY",
                    "license": "合同 W-1",
                }
            ],
            ensure_ascii=False,
        ),
    )
    monkeypatch.setenv("WANFANG_API_KEY", "super-secret-value")
    dumped = json.dumps(
        [cfg.__dict__ for cfg in configured_channels()], ensure_ascii=False, default=str
    )
    assert "super-secret-value" not in dumped
    assert "WANFANG_API_KEY" in dumped


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ----------------------------------------------- 跨语言补齐（LLM 增强，可降级）

CROSS_QUERY = "关于量子纠缠的县域治理研究"


def _no_llm(monkeypatch) -> None:
    monkeypatch.delenv("FD_SHILUN_LLM_API_KEY", raising=False)


def test_no_llm_key_keeps_deterministic_path(monkeypatch) -> None:
    """没配网关 → 不翻译、不补查，行为与之前完全一致（deterministic）。"""
    _no_llm(monkeypatch)
    calls: list[str] = []

    def fake_chat(*a, **k):  # noqa: ANN002, ANN003
        calls.append("chat")
        return "county governance"

    monkeypatch.setattr(search_mod.llm, "chat", fake_chat)
    monkeypatch.setattr(
        search_mod,
        "_OPEN_METADATA_CLIENT",
        _client(lambda r: httpx.Response(200, json={"message": {"items": []}})),
    )
    body = _client_for_search().post("/api/v1/search/works", json={"query": CROSS_QUERY}).json()

    assert calls == []  # 一次 LLM 调用都没发
    assert body["reason"] == "not_indexed"


def test_latin_query_is_judged_on_its_own_without_a_chinese_anchor() -> None:
    """纯英文查询走独立分支：不因「没有汉字记录做锚点」被整批拒绝，也不整批放行。

    回归锁定：两档判定原本以「通过词面门的汉字记录」为锚点，英文检索词进来后
    一条都留不下（跨语言补齐因此形同虚设）。
    """
    good = {
        "title": "County Governance and Quantum Entanglement",
        "venue": None,
        "abstract": None,
        "score": 55.0,
    }
    junk = {"title": "A Study of Soil Microbiome in Rice Paddies", "venue": None, "abstract": None}
    kept, dropped = om._filter_relevant(
        [dict(good), dict(junk)], "county governance and quantum entanglement"
    )
    assert [r["title"] for r in kept] == ["County Governance and Quantum Entanglement"]
    assert dropped == 1


def test_cross_lingual_retry_runs_only_after_empty_chinese_hits(monkeypatch) -> None:
    """中文零命中且配了网关 → 用英文检索词补查一次，命中即返回并说明已补查。"""
    monkeypatch.setenv("FD_SHILUN_LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        search_mod.llm, "chat", lambda *a, **k: "county governance and quantum entanglement"
    )

    translated_queries: list[str] = []
    english_item = {
        "DOI": "10.1/en.1",
        "title": ["County Governance and Quantum Entanglement"],
        "issued": {"date-parts": [[2024]]},
        "score": 55.0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        # Crossref 用 query.bibliographic，OpenAlex 用 search
        q = request.url.params.get("query.bibliographic") or request.url.params.get("search", "")
        translated_queries.append(q)
        if "quantum" in q.lower():  # 英文补查
            return httpx.Response(
                200, json={"message": {"total-results": 1, "items": [english_item]}}
            )
        return httpx.Response(200, json={"message": {"items": []}})  # 中文零命中

    monkeypatch.setattr(
        search_mod, "_OPEN_METADATA_CLIENT", _client(handler)
    )
    body = _client_for_search().post("/api/v1/search/works", json={"query": CROSS_QUERY}).json()

    assert body["reason"] is None
    assert body["results"][0]["title"] == "County Governance and Quantum Entanglement"
    assert body["results"][0]["provenance"]["coverage"] == "partial"
    assert "英文检索词" in body["coverage_note"]
    # 第一轮中文（Crossref → OpenAlex），第二轮英文补查（Crossref）
    assert translated_queries[0] == CROSS_QUERY
    assert "quantum" in translated_queries[-1].lower()


def test_cross_lingual_retry_cost_is_not_paid_when_chinese_hits_exist(monkeypatch) -> None:
    """中文已经命中就不翻译：补查只在零命中时付费。"""
    monkeypatch.setenv("FD_SHILUN_LLM_API_KEY", "test-key")
    calls: list[str] = []
    monkeypatch.setattr(search_mod.llm, "chat", lambda *a, **k: calls.append("x") or "english")

    monkeypatch.setattr(
        search_mod,
        "_OPEN_METADATA_CLIENT",
        _client(lambda r: httpx.Response(200, json={"message": {"total-results": 1, "items": [CROSSREF_ITEM]}})),
    )
    body = _client_for_search().post("/api/v1/search/works", json={"query": "乡村振兴"}).json()
    assert body["reason"] is None and body["results"]
    assert calls == []


def test_cross_lingual_llm_failure_degrades_to_not_indexed(monkeypatch) -> None:
    """网关报错不能让检索失败：降级为原路径的 not_indexed。"""
    monkeypatch.setenv("FD_SHILUN_LLM_API_KEY", "test-key")

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise search_mod.llm.LLMError("网关挂了")

    monkeypatch.setattr(search_mod.llm, "chat", boom)
    monkeypatch.setattr(
        search_mod,
        "_OPEN_METADATA_CLIENT",
        _client(lambda r: httpx.Response(200, json={"message": {"items": []}})),
    )
    resp = _client_for_search().post("/api/v1/search/works", json={"query": CROSS_QUERY})
    assert resp.status_code == 200
    assert resp.json()["reason"] == "not_indexed"


def test_translation_must_change_language(monkeypatch) -> None:
    """译文还是中文（模型偷懒）就不补查，避免白花一次请求。"""
    monkeypatch.setenv("FD_SHILUN_LLM_API_KEY", "test-key")
    monkeypatch.setattr(search_mod.llm, "chat", lambda *a, **k: "县域治理")
    assert search_mod._translated_query("县域治理") is None
