"""论文检索消费面（task 2.1/2.2）全离线测试。

不打真网：在 provider 的 HTTP 边界（OpenAlexProvider._get）打桩——
返回 fixture JSON（OpenAlex works 原始结构）或抛协议异常，走真实的
解析/分页/口径映射代码路径。
"""

from __future__ import annotations

import copy
import importlib.util
import sys
import types

# auth.py 依赖 fd_shilun_api.personal（个人层，兄弟任务交付）。
# 尚未落地时注入最小桩，保证本模块可导入；真实模块存在则完全不干预。
import fd_shilun_api  # noqa: F401 —— 先导入 parent，find_spec 才能解析子模块

if importlib.util.find_spec("fd_shilun_api.personal") is None:
    _stub = types.ModuleType("fd_shilun_api.personal")

    class _Conn:  # 最小连接桩：auth.authenticate 只用 connect/migrate/audit/close
        def close(self) -> None: ...

    _stub.connect = lambda: _Conn()  # type: ignore[attr-defined]
    _stub.migrate = lambda conn: None  # type: ignore[attr-defined]

    def _audit(conn, user_id, action, path, verdict, detail=None):  # noqa: ARG001
        return None

    _stub.audit = _audit  # type: ignore[attr-defined]
    sys.modules["fd_shilun_api.personal"] = _stub

import httpx as httpx_mod
from fastapi import FastAPI
from fastapi.testclient import TestClient

import fd_shilun_api.search as search_mod
from fd_open_data_protocol.openalex_provider import (
    OpenAlexQuotaExceeded,
    OpenAlexUnavailable,
)

# ---------------------------------------------------------------- fixtures

# OpenAlex /works 原始响应结构（search 与 filter=doi 共用此形状）
RAW_WORKS_PAYLOAD = {
    "meta": {"count": 2},
    "results": [
        {
            "id": "https://openalex.org/W1516100",
            "doi": "https://doi.org/10.1038/nature12373",
            "title": "Nanomachines: The machinery of life revisited",
            "publication_year": 2015,
            "cited_by_count": 1745,
            "primary_location": {"source": {"display_name": "Nature"}},
        },
        {
            "id": "https://openalex.org/W2100837268",
            "doi": "https://doi.org/10.1038/171737a0",
            "title": "Molecular structure of nucleic acids",
            "publication_year": 1953,
            "cited_by_count": 21876,
            "primary_location": {"source": {"display_name": "Nature"}},
        },
    ],
}

RAW_EMPTY_PAYLOAD = {"meta": {"count": 0}, "results": []}


def _make_client() -> TestClient:
    """带路由与鉴权放行的测试客户端。"""
    app = FastAPI()
    app.include_router(search_mod.router)
    app.dependency_overrides[search_mod.require_user] = lambda: "user_test"
    return TestClient(app)


def _patch_get(monkeypatch, payload=None, exc: Exception | None = None, capture: dict | None = None):
    """在 provider HTTP 边界打桩：返回 fixture 或抛异常，可选捕获请求参数。"""

    def fake_get(self, path, params=None):  # noqa: ANN001
        if capture is not None:
            capture["path"] = path
            capture["params"] = dict(params or {})
        if exc is not None:
            raise exc
        return copy.deepcopy(payload), {"x-ratelimit-limit": "10000"}

    monkeypatch.setattr(search_mod.OpenAlexProvider, "_get", fake_get)


# ------------------------------------------------------------------- tests

def test_query_returns_structured_results(monkeypatch) -> None:
    cap: dict = {}
    _patch_get(monkeypatch, payload=RAW_WORKS_PAYLOAD, capture=cap)
    resp = _make_client().post("/api/v1/search/works", json={"query": "nanomachines"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reason"] is None and body["results"], body
    item = body["results"][0]
    assert item["work_id"] == "W1516100"
    assert item["doi"] == "https://doi.org/10.1038/nature12373"
    assert item["title"] == "Nanomachines: The machinery of life revisited"
    assert item["year"] == 2015
    assert item["venue"] == "Nature"
    assert item["cited_by_count"] == 1745
    assert item["provenance"] == {
        "source": "openalex",
        "stable_id": "W1516100",
        "license": "CC0",
    }
    assert body["total"] == 2 and body["page"] == 1 and body["per_page"] == 10
    # 分页默认值透传到 OpenAlex
    assert cap["path"] == "works"
    assert cap["params"]["page"] == "1" and cap["params"]["per-page"] == "10"
    assert cap["params"]["search"] == "nanomachines"


def test_query_pagination_passthrough_and_caps(monkeypatch) -> None:
    cap: dict = {}
    _patch_get(monkeypatch, payload=RAW_WORKS_PAYLOAD, capture=cap)
    client = _make_client()
    resp = client.post("/api/v1/search/works", json={"query": "q", "page": 2, "per_page": 25})
    assert resp.status_code == 200
    assert resp.json()["page"] == 2 and resp.json()["per_page"] == 25
    assert cap["params"]["page"] == "2" and cap["params"]["per-page"] == "25"
    # 上限/下限：per_page>25、per_page<1、page<1 均拒绝
    assert client.post("/api/v1/search/works", json={"query": "q", "per_page": 26}).status_code == 422
    assert client.post("/api/v1/search/works", json={"query": "q", "per_page": 0}).status_code == 422
    assert client.post("/api/v1/search/works", json={"query": "q", "page": 0}).status_code == 422


def test_rerank_is_ignored_on_openalex_keyword_path(monkeypatch) -> None:
    """rerank 只作用于中文兜底：英文关键词已由 OpenAlex 排好序，不得再调模型。"""
    monkeypatch.setenv("FD_SHILUN_LLM_API_KEY", "test-key")
    _patch_get(monkeypatch, payload=RAW_WORKS_PAYLOAD)

    calls: list[object] = []

    def _must_not_call(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append((args, kwargs))
        raise AssertionError("OpenAlex 主路径不得调用模型")

    monkeypatch.setattr(search_mod.llm, "chat", _must_not_call)

    body = (
        _make_client()
        .post(
            "/api/v1/search/works",
            json={"query": "nanomachines", "rerank": True},
        )
        .json()
    )
    assert calls == []
    assert len(body["results"]) == 2


def test_request_query_doi_exclusivity(monkeypatch) -> None:
    _patch_get(monkeypatch, payload=RAW_WORKS_PAYLOAD)
    client = _make_client()
    assert client.post("/api/v1/search/works", json={}).status_code == 422
    both = {"query": "free will", "doi": "10.1038/nature12373"}
    assert client.post("/api/v1/search/works", json=both).status_code == 422
    blank = {"query": "   "}
    assert client.post("/api/v1/search/works", json=blank).status_code == 422


def test_doi_exact_lookup_normalizes_and_returns_single(monkeypatch) -> None:
    cap: dict = {}
    _patch_get(monkeypatch, payload=RAW_WORKS_PAYLOAD, capture=cap)
    resp = _make_client().post(
        "/api/v1/search/works",
        json={"doi": "https://doi.org/10.1038/NATURE12373"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1 and body["total"] == 1
    item = body["results"][0]
    assert item["work_id"] == "W1516100"
    assert item["venue"] == "Nature" and item["year"] == 2015
    assert item["provenance"]["stable_id"] == "W1516100"
    # DOI 归一化（去前缀、小写）后走精确 filter
    assert cap["params"]["filter"] == "doi:10.1038/nature12373"


def test_doi_not_indexed(monkeypatch) -> None:
    _patch_get(monkeypatch, payload=RAW_EMPTY_PAYLOAD)  # filter 无命中 → ValueError
    resp = _make_client().post("/api/v1/search/works", json={"doi": "10.9999/nope"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert body["reason"] == "not_indexed"
    assert "10.9999/nope" in (body["detail"] or "")


def test_query_no_hits_is_not_indexed(monkeypatch) -> None:
    _patch_get(monkeypatch, payload=RAW_EMPTY_PAYLOAD)
    # 非中文 query：走 OpenAlex 默认通道（中文 query 按 spec 改走授权通道，
    # 见 tests/test_channels.py）
    resp = _make_client().post("/api/v1/search/works", json={"query": "zzz nonexistent topic"})
    body = resp.json()
    assert body["results"] == [] and body["total"] == 0
    assert body["reason"] == "not_indexed"


def test_page_beyond_hits_not_indexed(monkeypatch) -> None:
    payload = copy.deepcopy(RAW_WORKS_PAYLOAD)
    payload["results"] = []  # 该页无行，但 total>0
    _patch_get(monkeypatch, payload=payload)
    resp = _make_client().post(
        "/api/v1/search/works", json={"query": "q", "page": 99}
    )
    body = resp.json()
    assert body["results"] == [] and body["reason"] == "not_indexed"
    assert "total=2" in (body["detail"] or "")


def test_reason_quota(monkeypatch) -> None:
    _patch_get(monkeypatch, exc=OpenAlexQuotaExceeded("HTTP 429"))
    resp = _make_client().post("/api/v1/search/works", json={"query": "q"})
    assert resp.json()["reason"] == "quota"
    resp = _make_client().post("/api/v1/search/works", json={"doi": "10.1/x"})
    assert resp.json()["reason"] == "quota"


def test_reason_upstream(monkeypatch) -> None:
    _patch_get(monkeypatch, exc=OpenAlexUnavailable("HTTP 503"))
    resp = _make_client().post("/api/v1/search/works", json={"query": "q"})
    assert resp.json()["reason"] == "upstream"
    resp = _make_client().post("/api/v1/search/works", json={"doi": "10.1/x"})
    assert resp.json()["reason"] == "upstream"


def test_reason_unreachable_on_httpx_timeout(monkeypatch) -> None:
    # httpx 超时逃逸出 provider（provider 用 urllib，兜外部注入 httpx 的场景）
    _patch_get(monkeypatch, exc=httpx_mod.ConnectTimeout("timed out"))
    resp = _make_client().post("/api/v1/search/works", json={"query": "q"})
    body = resp.json()
    assert body["results"] == [] and body["reason"] == "unreachable"
    assert "ConnectTimeout" in (body["detail"] or "")


def test_reason_unreachable_on_builtin_network_error(monkeypatch) -> None:
    _patch_get(monkeypatch, exc=TimeoutError("connect timeout"))
    resp = _make_client().post("/api/v1/search/works", json={"doi": "10.1/x"})
    assert resp.json()["reason"] == "unreachable"


def test_reason_unexpected_exception_maps_upstream(monkeypatch) -> None:
    _patch_get(monkeypatch, exc=RuntimeError("boom"))
    resp = _make_client().post("/api/v1/search/works", json={"query": "q"})
    body = resp.json()
    assert body["results"] == [] and body["reason"] == "upstream"
    assert "RuntimeError" in (body["detail"] or "")


def test_failure_keeps_pagination_context(monkeypatch) -> None:
    _patch_get(monkeypatch, exc=OpenAlexQuotaExceeded("429"))
    resp = _make_client().post(
        "/api/v1/search/works", json={"query": "q", "page": 3, "per_page": 20}
    )
    body = resp.json()
    assert body["reason"] == "quota" and body["page"] == 3 and body["per_page"] == 20


def test_auth_required_without_token(monkeypatch) -> None:
    """不走 dependency override：未带 token 必须 401（鉴权真实接线）。"""
    monkeypatch.delenv("FD_SHILUN_AUTH_DEV_TOKENS", raising=False)
    app = FastAPI()
    app.include_router(search_mod.router)
    client = TestClient(app)
    resp = client.post("/api/v1/search/works", json={"query": "q"})
    assert resp.status_code == 401
