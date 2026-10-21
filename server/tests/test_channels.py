"""中文文献授权通道（tasks 2.1-2.3）全离线测试。

不配置真实厂商、不打网络：
- 配置/沙箱路径直接读环境变量 FD_SHILUN_CN_CHANNELS；
- HTTP 适配器用 httpx.MockTransport 在传输层打桩，走真实的解析/鉴权/
  失败归因代码路径；
- 路由路径用 sentinel 证明中文检索不会构造 OpenAlex provider。
"""

from __future__ import annotations

import json

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import fd_shilun_api.channels as channels_mod
import fd_shilun_api.search as search_mod
from fd_shilun_api.channels import (
    ENV_CHANNELS,
    ChannelResult,
    HttpSearchChannel,
    configured_channels,
    has_authorized_channel,
    search_chinese,
    usable_channels,
)

SANDBOX_ID = "sandbox-cn"
SANDBOX_LICENSE = "测试授权依据（sandbox-fixture，不构成对外授权）"

SANDBOX_ENTRY = {
    "id": SANDBOX_ID,
    "label": "沙箱中文通道",
    "kind": "sandbox",
    "license": SANDBOX_LICENSE,
    "fixtures": [
        {
            "query_substring": "乡村振兴",
            "records": [
                {
                    "标识": "CN-001",
                    "题名": "乡村振兴战略下的县域治理",
                    "作者": ["张三", "李四"],
                    "年份": 2023,
                    "来源": "中国行政管理",
                    "摘要": "基于县域样本的实证研究。",
                    "doi": "10.1234/x",
                    "url": "https://example.org/x",
                },
                {
                    "标识": "CN-002",
                    "题名": "乡村产业振兴的路径",
                    "作者": "王五;赵六",
                    "年份": "2021年",
                    "来源": "农业经济问题",
                    "摘要": "案例比较。",
                    "doi": "10.1234/y",
                    "url": "https://example.org/y",
                },
            ],
        }
    ],
}

HTTP_ENTRY = {
    "id": "wanfang",
    "label": "万方数据",
    "kind": "http",
    "base_url": "https://api.example.com/v1/search",
    "api_key_env": "WANFANG_API_KEY",
    "license": "万方合同 W-2026-001",
    "auth_scheme": "header",
    "header_name": "X-API-KEY",
}


def _set_channels(monkeypatch, entries: list[dict]) -> None:
    monkeypatch.setenv(ENV_CHANNELS, json.dumps(entries, ensure_ascii=False))


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(search_mod.router)
    app.dependency_overrides[search_mod.require_user] = lambda: "user_test"
    return TestClient(app)


def _forbid_provider(monkeypatch) -> None:
    """中文路径一旦构造 OpenAlex provider 就立即失败。"""

    def _boom():  # noqa: ANN202
        raise AssertionError("中文检索不得构造 OpenAlex provider")

    monkeypatch.setattr(search_mod, "_new_provider", _boom)


def _http_config(monkeypatch, entry: dict = HTTP_ENTRY):
    monkeypatch.setenv(entry["api_key_env"], "secret-key")
    configs = configured_channels(json.dumps([entry], ensure_ascii=False))
    assert len(configs) == 1 and configs[0].usable, configs
    return configs[0]


# ------------------------------------------------------------ 未授权路径


def test_no_config_returns_no_authorized_channel(monkeypatch) -> None:
    monkeypatch.delenv(ENV_CHANNELS, raising=False)
    assert configured_channels() == []
    assert usable_channels() == []
    assert has_authorized_channel() is False

    result = search_chinese("乡村振兴")
    assert result["records"] == []
    assert result["reason"] == "no_authorized_channel"
    assert result["channel"] is None
    assert result["authorization_basis"] is None
    assert ENV_CHANNELS in (result["detail"] or "")


def test_explicit_empty_channels_is_no_authorized_channel(monkeypatch) -> None:
    _set_channels(monkeypatch, [SANDBOX_ENTRY])
    result = search_chinese("乡村振兴", channels=[])
    assert result["reason"] == "no_authorized_channel"


def test_malformed_config_degrades_without_raising(monkeypatch) -> None:
    monkeypatch.setenv(ENV_CHANNELS, "[{not json")
    assert configured_channels() == []
    result = search_chinese("乡村振兴")
    assert result["records"] == []
    assert result["reason"] == "no_authorized_channel"
    assert "JSON" in (result["detail"] or "")


def test_non_array_config_is_unusable(monkeypatch) -> None:
    monkeypatch.setenv(ENV_CHANNELS, json.dumps({"id": "wanfang"}))
    result = search_chinese("乡村振兴")
    assert result["reason"] == "no_authorized_channel"
    assert "数组" in (result["detail"] or "")


def test_http_channel_with_unset_api_key_is_credential_pending(monkeypatch) -> None:
    """环境变量未设置 ≠ 配置写坏：通道进入「凭据待提供」，可由调用方 BYOK 补齐。"""
    monkeypatch.delenv("WANFANG_API_KEY", raising=False)
    _set_channels(monkeypatch, [HTTP_ENTRY])

    configs = configured_channels()
    assert len(configs) == 1
    assert configs[0].usable is True
    assert configs[0].credential_pending is True
    assert configs[0].problem is None
    # 没有凭据就构造不出适配器：不能「配了但跑起来再看」
    assert usable_channels() == []
    assert has_authorized_channel() is False

    result = search_chinese("乡村振兴")
    assert result["records"] == []
    assert result["reason"] == "no_authorized_channel"
    assert "WANFANG_API_KEY" in (result["detail"] or "")
    assert "凭据" in (result["detail"] or "")


def test_explicit_credentials_make_pending_channel_usable(monkeypatch) -> None:
    """BYOK：显式凭据让「环境变量未设置」的通道当次可用。"""
    monkeypatch.delenv("WANFANG_API_KEY", raising=False)
    _set_channels(monkeypatch, [HTTP_ENTRY])

    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(
            200,
            json={"results": [{"标识": "CN-9", "题名": "县域治理", "年份": 2022}]},
        )

    channel = HttpSearchChannel(
        configured_channels()[0],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        api_key="byok-secret",
    )
    result = search_chinese("乡村振兴", channels=[channel])

    assert result["reason"] is None
    assert result["records"][0]["title"] == "县域治理"
    # HTTP_ENTRY 走 auth_scheme=header / X-API-KEY
    assert seen and seen[0]["x-api-key"] == "byok-secret"
    # 凭据不进配置对象，也不进结果
    assert configured_channels()[0].api_key_env == "WANFANG_API_KEY"
    assert "byok-secret" not in json.dumps(result, ensure_ascii=False)


def test_explicit_credentials_take_precedence_over_env(monkeypatch) -> None:
    """优先级：显式凭据 > 环境变量（同一通道两者都在时用显式的）。"""
    monkeypatch.setenv("WANFANG_API_KEY", "env-secret")
    _set_channels(monkeypatch, [HTTP_ENTRY])

    config = configured_channels()[0]
    assert channels_mod._resolve_api_key(config, {"wanfang": "explicit-secret"}) == (
        "explicit-secret"
    )
    assert channels_mod._resolve_api_key(config, None) == "env-secret"
    # 显式传空串视为未提供，回落到环境变量
    assert channels_mod._resolve_api_key(config, {"wanfang": "   "}) == "env-secret"

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("x-api-key", ""))
        return httpx.Response(200, json={"results": []})

    channel = HttpSearchChannel(
        config,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        api_key="explicit-secret",
    )
    channel.search("x")
    assert sent == ["explicit-secret"]



def test_http_channel_with_api_key_is_structurally_usable(monkeypatch) -> None:
    monkeypatch.setenv("WANFANG_API_KEY", "secret-key")
    _set_channels(monkeypatch, [HTTP_ENTRY])
    assert has_authorized_channel() is True
    channels = usable_channels()
    assert len(channels) == 1 and channels[0].id == "wanfang"
    assert channels[0].license == HTTP_ENTRY["license"]


def test_missing_required_fields_mark_channel_unusable(monkeypatch) -> None:
    broken = {"id": "broken", "kind": "http", "license": "x"}  # 缺 base_url/api_key_env
    _set_channels(monkeypatch, [broken])
    config = configured_channels()[0]
    assert config.usable is False
    assert "base_url" in (config.problem or "")
    assert "api_key_env" in (config.problem or "")
    assert search_chinese("乡村振兴")["reason"] == "no_authorized_channel"


# -------------------------------------------------------------- 沙箱路径


def test_sandbox_hit_returns_records_channel_and_basis(monkeypatch) -> None:
    _set_channels(monkeypatch, [SANDBOX_ENTRY])
    result = search_chinese("乡村振兴")

    assert result["reason"] is None and result["detail"] is None
    assert result["channel"] == SANDBOX_ID
    assert result["authorization_basis"] == SANDBOX_LICENSE
    assert result["total"] == 2
    assert len(result["records"]) == 2

    first = result["records"][0]
    assert first["stable_id"] == "CN-001"
    assert first["title"] == "乡村振兴战略下的县域治理"
    assert first["authors"] == ["张三", "李四"]
    assert first["year"] == 2023
    assert first["venue"] == "中国行政管理"
    assert first["abstract"] == "基于县域样本的实证研究。"
    assert first["doi"] == "10.1234/x"
    assert first["url"] == "https://example.org/x"

    # 中文别名同样归一化：作者字符串按分隔符拆分、年份字符串取四位数字
    second = result["records"][1]
    assert second["authors"] == ["王五", "赵六"]
    assert second["year"] == 2021


def test_sandbox_no_match_is_not_indexed(monkeypatch) -> None:
    _set_channels(monkeypatch, [SANDBOX_ENTRY])
    result = search_chinese("量子计算")
    assert result["records"] == []
    assert result["reason"] == "not_indexed"
    assert result["channel"] is None
    assert "量子计算" in (result["detail"] or "")


def test_sandbox_pagination(monkeypatch) -> None:
    _set_channels(monkeypatch, [SANDBOX_ENTRY])
    second_page = search_chinese("乡村振兴", page=2, per_page=1)
    assert len(second_page["records"]) == 1
    assert second_page["records"][0]["stable_id"] == "CN-002"
    assert second_page["total"] == 2

    beyond = search_chinese("乡村振兴", page=9, per_page=10)
    assert beyond["records"] == [] and beyond["reason"] == "not_indexed"
    assert "total=2" in (beyond["detail"] or "")


# -------------------------------------------------------- 失败归因与顺序


class _FakeChannel:
    """按预设返回结果的协议桩，用于验证多通道顺序与归因优先级。"""

    def __init__(
        self,
        channel_id: str,
        *,
        failure: str | None = None,
        records: list[dict] | None = None,
        detail: str | None = None,
    ) -> None:
        self.id = channel_id
        self.label = channel_id
        self.license = f"{channel_id}-basis"
        self._failure = failure
        self._records = records or []
        self._detail = detail

    def search(self, query: str, *, page: int = 1, per_page: int = 10) -> ChannelResult:
        return ChannelResult(
            records=list(self._records),
            channel=self.id,
            authorization_basis=self.license,
            total=len(self._records),
            failure=self._failure,  # type: ignore[arg-type]
            detail=self._detail,
        )


def test_failure_preference_prefers_actionable_reason() -> None:
    channels = [
        _FakeChannel("a", failure="not_indexed"),
        _FakeChannel("b", failure="upstream", detail="HTTP 503"),
    ]
    result = search_chinese("x", channels=channels)
    assert result["reason"] == "upstream"
    assert "a: not_indexed" in (result["detail"] or "")
    assert "b: upstream" in (result["detail"] or "")


def test_first_channel_with_hits_wins_in_configured_order() -> None:
    channels = [
        _FakeChannel("a", failure="not_indexed"),
        _FakeChannel(
            "b",
            records=[{"stable_id": "B-1", "doi": None, "title": "t"}],
        ),
        _FakeChannel(
            "c",
            records=[{"stable_id": "C-1", "doi": None, "title": "t"}],
        ),
    ]
    result = search_chinese("x", channels=channels)
    assert result["reason"] is None
    assert result["channel"] == "b"
    assert result["authorization_basis"] == "b-basis"
    assert [r["stable_id"] for r in result["records"]] == ["B-1"]


def test_adapter_exception_maps_upstream() -> None:
    class _Boom:
        id = "boom"
        label = "boom"
        license = "boom-basis"

        def search(self, query, *, page=1, per_page=10):
            raise RuntimeError("kaboom")

    result = search_chinese("x", channels=[_Boom()])
    assert result["reason"] == "upstream"
    assert "RuntimeError" in (result["detail"] or "")


# ------------------------------------------------------- HTTP 适配器（离线）


def test_http_channel_header_auth_and_chinese_payload(monkeypatch) -> None:
    config = _http_config(monkeypatch)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("x-api-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {
                    "records": [
                        {
                            "题名": "乡村治理研究",
                            "作者": "张三;李四",
                            "年份": "2022",
                            "来源": "某学报",
                            "摘要": "摘要",
                            "标识": "CN-9",
                        }
                    ],
                    "total": 1,
                }
            },
        )

    channel = HttpSearchChannel(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = channel.search("乡村振兴", page=2, per_page=5)

    assert captured["key"] == "secret-key"
    assert captured["body"] == {"query": "乡村振兴", "page": 2, "per_page": 5}
    assert result.failure is None
    assert result.channel == "wanfang" and result.authorization_basis == config.license
    assert result.total == 1
    assert result.records[0]["stable_id"] == "CN-9"
    assert result.records[0]["authors"] == ["张三", "李四"]
    assert result.records[0]["year"] == 2022


def test_http_channel_bearer_auth_and_english_fields(monkeypatch) -> None:
    entry = dict(HTTP_ENTRY, auth_scheme="bearer")
    entry.pop("header_name")
    config = _http_config(monkeypatch, entry)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "W1", "title": "A paper", "publication_year": 2020}
                ],
                "total": 1,
            },
        )

    channel = HttpSearchChannel(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = channel.search("x")
    assert captured["auth"] == "Bearer secret-key"
    assert result.failure is None
    assert result.records[0]["stable_id"] == "W1"
    assert result.records[0]["year"] == 2020


def test_http_channel_quota_on_429(monkeypatch) -> None:
    config = _http_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "too many"})

    result = HttpSearchChannel(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    ).search("x")
    assert result.records == [] and result.failure == "quota"


def test_http_channel_unreachable_on_timeout(monkeypatch) -> None:
    config = _http_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    result = HttpSearchChannel(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    ).search("x")
    assert result.records == [] and result.failure == "unreachable"


def test_http_channel_unparseable_payload_is_upstream(monkeypatch) -> None:
    config = _http_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    result = HttpSearchChannel(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    ).search("x")
    assert result.records == [] and result.failure == "upstream"


# ------------------------------------------------------------- 路由接线


def _stub_open_metadata(monkeypatch, *, crossref: dict | None = None, openalex: dict | None = None) -> list[str]:
    """把开放元数据兜底的 HTTP 换成 MockTransport，返回被请求的 URL 列表。

    crossref/openalex 传 None 表示该源「无命中」，从而覆盖「两者都查不到」的路径。
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "crossref" in request.url.host:
            payload = crossref if crossref is not None else {"message": {"items": []}}
            return httpx.Response(200, json=payload)
        payload = openalex if openalex is not None else {"results": [], "meta": {"count": 0}}
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        search_mod,
        "_OPEN_METADATA_CLIENT",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return seen


CROSSREF_ONE = {
    "message": {
        "total-results": 1,
        "items": [
            {
                "DOI": "10.1234/cn.2023.001",
                "title": ["乡村振兴战略下的县域治理"],
                "author": [{"given": "三", "family": "张"}],
                "issued": {"date-parts": [[2023, 5]]},
                "container-title": ["中国行政管理"],
                "URL": "https://doi.org/10.1234/cn.2023.001",
            }
        ],
    }
}


CROSSREF_TWO = {
    "message": {
        "total-results": 2,
        "items": [
            {
                "DOI": "10.1234/cn.2023.001",
                "title": ["乡村振兴战略下的县域治理"],
                "author": [{"given": "三", "family": "张"}],
                "issued": {"date-parts": [[2023, 5]]},
                "container-title": ["中国行政管理"],
            },
            {
                "DOI": "10.1234/cn.2022.002",
                "title": ["乡村振兴背景下的财政支农机制"],
                "author": [{"given": "五", "family": "王"}],
                "issued": {"date-parts": [[2022, 1]]},
                "container-title": ["农业经济问题"],
            },
        ],
    }
}


def test_route_chinese_without_channel_falls_back_to_open_metadata(monkeypatch) -> None:
    """无授权通道 → 开放元数据兜底，逐条标 partial/metadata_only + coverage_note。"""
    monkeypatch.delenv(ENV_CHANNELS, raising=False)
    monkeypatch.delenv(search_mod.CN_OPEN_FALLBACK_ENV, raising=False)
    _forbid_provider(monkeypatch)
    seen = _stub_open_metadata(monkeypatch, crossref=CROSSREF_ONE)

    resp = _make_client().post("/api/v1/search/works", json={"query": "乡村振兴"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["reason"] is None
    assert body["total"] == 1
    assert body["page"] == 1 and body["per_page"] == 10
    assert "覆盖不完整" in body["coverage_note"]

    item = body["results"][0]
    assert item["work_id"] == "10.1234/cn.2023.001"
    assert item["title"] == "乡村振兴战略下的县域治理"
    assert item["year"] == 2023 and item["venue"] == "中国行政管理"
    prov = item["provenance"]
    assert {k: prov[k] for k in ("source", "stable_id", "license", "coverage", "metadata_only")} == {
        "source": "crossref",
        "stable_id": "10.1234/cn.2023.001",
        "license": "metadata-only",
        "coverage": "partial",
        "metadata_only": True,
    }
    # 部分覆盖要摊开：本条命中查询里的哪些词（「乡村振兴」的三个二元组）
    assert set(prov["matched_terms"]) == {"乡村", "村振", "振兴"}
    assert prov["coverage_ratio"] == 1.0
    # 兜底只问公开元数据，绝不构造 OpenAlex provider（那是主路径，不用于中文兜底）
    assert seen and "crossref" in seen[0]


def test_route_chinese_open_metadata_miss_is_not_indexed(monkeypatch) -> None:
    """两者都无命中 → not_indexed + 覆盖说明；不得返回 no_authorized_channel。"""
    monkeypatch.delenv(ENV_CHANNELS, raising=False)
    monkeypatch.delenv(search_mod.CN_OPEN_FALLBACK_ENV, raising=False)
    _forbid_provider(monkeypatch)
    _stub_open_metadata(monkeypatch)  # 两个源都空

    body = (
        _make_client()
        .post("/api/v1/search/works", json={"query": "乡村振兴"})
        .json()
    )
    assert body["results"] == []
    assert body["reason"] == "not_indexed"
    assert "覆盖不完整" in (body["detail"] or "")
    assert "no_authorized_channel" not in json.dumps(body, ensure_ascii=False)


def test_rerank_without_llm_key_makes_no_model_call(monkeypatch) -> None:
    """未配置网关：rerank=True 不得发起任何模型调用，结果与确定性路径一致。"""
    monkeypatch.delenv(ENV_CHANNELS, raising=False)
    monkeypatch.delenv(search_mod.CN_OPEN_FALLBACK_ENV, raising=False)
    monkeypatch.delenv("FD_SHILUN_LLM_API_KEY", raising=False)
    _forbid_provider(monkeypatch)
    _stub_open_metadata(monkeypatch, crossref=CROSSREF_TWO)

    calls: list[object] = []

    def _must_not_call(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append((args, kwargs))
        raise AssertionError("未配置网关时不得调用模型")

    monkeypatch.setattr(search_mod.llm, "chat", _must_not_call)

    body = (
        _make_client()
        .post(
            "/api/v1/search/works",
            json={"query": "乡村振兴", "rerank": True},
        )
        .json()
    )
    assert calls == []
    assert len(body["results"]) == 2
    assert body["reason"] is None


def test_rerank_keeps_only_model_selected_candidates(monkeypatch) -> None:
    """配了网关：按模型给出的下标过滤；不勾精排则不花这次调用。"""
    monkeypatch.delenv(ENV_CHANNELS, raising=False)
    monkeypatch.delenv(search_mod.CN_OPEN_FALLBACK_ENV, raising=False)
    monkeypatch.setenv("FD_SHILUN_LLM_API_KEY", "test-key")
    _forbid_provider(monkeypatch)
    _stub_open_metadata(monkeypatch, crossref=CROSSREF_TWO)

    seen: list[dict] = []

    def _pick_second(messages, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.append({"messages": messages, **kwargs})
        return "[1]"

    monkeypatch.setattr(search_mod.llm, "chat", _pick_second)
    client = _make_client()

    body = client.post(
        "/api/v1/search/works", json={"query": "乡村振兴", "rerank": True}
    ).json()
    assert [r["title"] for r in body["results"]] == ["乡村振兴背景下的财政支农机制"]
    # 一次降温的取舍调用，且把检索词与候选题名都交给了模型
    assert len(seen) == 1 and seen[0]["temperature"] == 0
    prompt = json.dumps(seen[0]["messages"], ensure_ascii=False)
    assert "乡村振兴" in prompt and "财政支农" in prompt

    # 精排是显式可选项：不勾就不该产生模型调用
    body = client.post("/api/v1/search/works", json={"query": "乡村振兴"}).json()
    assert len(body["results"]) == 2 and len(seen) == 1


def test_rerank_garbage_output_keeps_all_candidates(monkeypatch) -> None:
    """模型输出不可解析/越界/空数组/调用失败 → 一律保留全部候选。

    空结果的结论只能由确定性通道给出：模型答非所问时不能把一次检索清空。
    """
    monkeypatch.delenv(ENV_CHANNELS, raising=False)
    monkeypatch.delenv(search_mod.CN_OPEN_FALLBACK_ENV, raising=False)
    monkeypatch.setenv("FD_SHILUN_LLM_API_KEY", "test-key")
    _forbid_provider(monkeypatch)
    _stub_open_metadata(monkeypatch, crossref=CROSSREF_TWO)
    client = _make_client()

    for answer in ("我觉得第二条更相关", "[0, 99]", '[0, "x"]', "[]", "[true]"):
        monkeypatch.setattr(search_mod.llm, "chat", lambda *a, _a=answer, **k: _a)
        body = client.post(
            "/api/v1/search/works", json={"query": "乡村振兴", "rerank": True}
        ).json()
        assert len(body["results"]) == 2, answer

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise search_mod.llm.LLMError("网关挂了")

    monkeypatch.setattr(search_mod.llm, "chat", _boom)
    body = client.post(
        "/api/v1/search/works", json={"query": "乡村振兴", "rerank": True}
    ).json()
    assert len(body["results"]) == 2


def test_route_chinese_fallback_switch_off_restores_old_behaviour(monkeypatch) -> None:
    """回滚档：FD_SHILUN_CN_OPEN_FALLBACK=0 → 回到 no_authorized_channel，且不发兜底请求。"""
    monkeypatch.delenv(ENV_CHANNELS, raising=False)
    monkeypatch.setenv(search_mod.CN_OPEN_FALLBACK_ENV, "0")
    _forbid_provider(monkeypatch)
    seen = _stub_open_metadata(monkeypatch, crossref=CROSSREF_ONE)

    body = (
        _make_client()
        .post("/api/v1/search/works", json={"query": "乡村振兴"})
        .json()
    )
    assert body["results"] == []
    assert body["reason"] == "no_authorized_channel"
    assert seen == []  # 关闭时一次外部请求都不发
    assert "coverage_note" not in body


def test_route_chinese_channel_hit_does_not_fall_back(monkeypatch) -> None:
    """授权通道命中即返回，不再问开放元数据，也不标 partial。"""
    _set_channels(monkeypatch, [SANDBOX_ENTRY])
    _forbid_provider(monkeypatch)
    seen = _stub_open_metadata(monkeypatch, crossref=CROSSREF_ONE)

    body = (
        _make_client()
        .post("/api/v1/search/works", json={"query": "乡村振兴"})
        .json()
    )
    assert body["reason"] is None and len(body["results"]) == 2
    assert seen == []
    assert "coverage_note" not in body
    assert "coverage" not in body["results"][0]["provenance"]


def test_route_chinese_byok_credentials_from_request(monkeypatch, capsys) -> None:
    """BYOK：凭据随请求传入即可完成检索，响应与输出流里都不出现 key 明文。"""
    monkeypatch.delenv("WANFANG_API_KEY", raising=False)
    _set_channels(monkeypatch, [HTTP_ENTRY])
    _forbid_provider(monkeypatch)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-api-key", ""))
        return httpx.Response(
            200,
            json={"results": [{"标识": "WF-1", "题名": "县域财政", "年份": 2021}]},
        )

    # 只换传输层：凭据解析、鉴权头拼装、解析路径都走真实代码
    original = channels_mod.HttpSearchChannel.__init__

    def patched(self, config, *, client=None, api_key=None):  # noqa: ANN001, ANN202
        original(
            self,
            config,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            api_key=api_key,
        )

    monkeypatch.setattr(channels_mod.HttpSearchChannel, "__init__", patched)

    resp = _make_client().post(
        "/api/v1/search/works",
        json={"query": "县域财政", "channel_credentials": {"wanfang": "byok-key-123"}},
    )
    captured = capsys.readouterr()
    assert resp.status_code == 200
    body = resp.json()
    assert body["reason"] is None
    assert body["results"][0]["provenance"]["authorization"]["channel"] == "wanfang"
    assert seen == ["byok-key-123"]
    # 凭据只该出现在发出去的请求头里：响应体、stdout、stderr 都不许有
    assert "byok-key-123" not in resp.text
    assert "byok-key-123" not in captured.out
    assert "byok-key-123" not in captured.err


def test_route_chinese_with_sandbox_channel_returns_hits(monkeypatch) -> None:
    _set_channels(monkeypatch, [SANDBOX_ENTRY])
    _forbid_provider(monkeypatch)

    resp = _make_client().post("/api/v1/search/works", json={"query": "乡村振兴"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reason"] is None and body["total"] == 2
    assert len(body["results"]) == 2

    item = body["results"][0]
    assert item["work_id"] == "CN-001"
    assert item["title"] == "乡村振兴战略下的县域治理"
    assert item["year"] == 2023 and item["venue"] == "中国行政管理"
    assert item["provenance"] == {
        "source": SANDBOX_ID,
        "stable_id": "CN-001",
        "license": SANDBOX_LICENSE,
        "authorization": {"channel": SANDBOX_ID, "basis": SANDBOX_LICENSE},
    }


def test_route_non_chinese_query_still_uses_openalex(monkeypatch) -> None:
    # 即便配置了中文通道，非中文 query 仍走 OpenAlex 默认通道
    _set_channels(monkeypatch, [SANDBOX_ENTRY])
    captured: dict = {}

    def fake_get(self, path, params=None):  # noqa: ANN001
        captured["path"] = path
        captured["search"] = (params or {}).get("search")
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
    body = (
        _make_client()
        .post("/api/v1/search/works", json={"query": "rural revitalization"})
        .json()
    )
    assert body["reason"] is None
    assert body["results"][0]["provenance"]["source"] == "openalex"
    # OpenAlex 路径不出现授权字段：既有响应形状不变
    assert "authorization" not in body["results"][0]["provenance"]
    assert captured == {"path": "works", "search": "rural revitalization"}


def test_cjk_detection() -> None:
    assert search_mod._has_cjk("乡村振兴")
    assert search_mod._has_cjk("县域治理 2023")
    assert not search_mod._has_cjk("rural revitalization")
    assert not search_mod._has_cjk("10.1038/nature12373")
    assert not search_mod._has_cjk("")
