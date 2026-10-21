"""writing 模块测试（tasks 4.1/4.2/4.3）：全离线、临时 DB。

鉴权用 dependency_overrides 替身（真实鉴权链依赖并行交付的 personal 模块，
其行为由 auth 侧测试覆盖）；LLM 增强路径确保无 KEY，走确定性引擎。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fd_shilun_api import writing


class _AuthHolder:
    """测试内切换当前用户，验证归属隔离。"""

    def __init__(self) -> None:
        self.user = "user_a"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_SHILUN_DB", str(tmp_path / "writing.db"))
    monkeypatch.delenv("FD_SHILUN_LLM_API_KEY", raising=False)
    holder = _AuthHolder()
    app = FastAPI()
    app.include_router(writing.router)
    app.dependency_overrides[writing.require_user] = lambda: holder.user
    return TestClient(app), holder


def _mk_project(client: TestClient, title: str = "毕业论文初稿", kind: str = "thesis") -> dict:
    resp = client.post("/api/v1/writing/projects", json={"title": title, "kind": kind})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _cite(client: TestClient, pid: str, ref_id: str, meta: dict, kind: str = "openalex") -> dict:
    resp = client.post(
        f"/api/v1/writing/projects/{pid}/citations",
        json={"kind": kind, "ref_id": ref_id, "meta": meta},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---- 1. 项目 CRUD + 版本快照 + 归属隔离 ----


def test_project_crud_versions_and_isolation(client) -> None:
    tc, holder = client
    proj = _mk_project(tc)
    assert proj["kind"] == "thesis" and proj["id"]

    listed = tc.get("/api/v1/writing/projects").json()
    assert listed["count"] == 1 and listed["results"][0]["id"] == proj["id"]

    v = tc.post(
        f"/api/v1/writing/projects/{proj['id']}/versions",
        json={"snapshot": {"text": "初稿正文……", "misc": 1}, "note": "v1"},
    )
    assert v.status_code == 201, v.text
    versions = tc.get(f"/api/v1/writing/projects/{proj['id']}/versions").json()
    assert versions["count"] == 1
    assert versions["results"][0]["snapshot"] == {"text": "初稿正文……", "misc": 1}

    # 非法 kind 拒绝
    assert tc.post("/api/v1/writing/projects", json={"title": "x", "kind": "paper"}).status_code == 422

    # 隔离：user_b 看不到 user_a 的项目/版本，也不能写入
    holder.user = "user_b"
    assert tc.get("/api/v1/writing/projects").json()["count"] == 0
    assert tc.get(f"/api/v1/writing/projects/{proj['id']}/versions").status_code == 404
    assert (
        tc.post(
            f"/api/v1/writing/projects/{proj['id']}/citations",
            json={"kind": "openalex", "ref_id": "W1", "meta": {}},
        ).status_code
        == 404
    )


def test_citation_collection_idempotent(client) -> None:
    tc, _ = client
    proj = _mk_project(tc)
    meta = {"type": "journal-article", "title": "T", "year": 2023}
    first = _cite(tc, proj["id"], "W123", meta)
    assert first["created"] is True
    again = _cite(tc, proj["id"], "W123", meta)
    assert again["created"] is False and again["id"] == first["id"]
    listed = tc.get(f"/api/v1/writing/projects/{proj['id']}/citations").json()
    assert listed["count"] == 1
    # 非法 kind / 非法 format
    assert (
        tc.post(
            f"/api/v1/writing/projects/{proj['id']}/citations",
            json={"kind": "wiki", "ref_id": "W9", "meta": {}},
        ).status_code
        == 422
    )
    assert (
        tc.get(f"/api/v1/writing/projects/{proj['id']}/citations", params={"format": "mla"}).status_code
        == 400
    )


# ---- 3. 引用格式化：中英混合，期刊 + 预印本 ----

EN_JOURNAL = {
    "type": "journal-article",
    "title": "Retrieval-augmented generation for knowledge-intensive tasks",
    "authors": ["Zhihong Shen", "Beining Cui", "Mingyang Long", "Daxin Jiang"],
    "venue": "Expert Systems",
    "year": 2022,
    "volume": 39,
    "issue": 1,
    "pages": "e12770",
    "doi": "10.1111/exsy.12770",
}
ZH_JOURNAL = {
    "type": "journal-article",
    "title": "基于深度学习的中文分词方法研究",
    "authors": ["王晓芳", "李明"],
    "venue": "中文信息学报",
    "year": 2020,
    "volume": 32,
    "issue": 4,
    "pages": "45-52",
}
ZH_JOURNAL_MANY = {
    "type": "journal-article",
    "title": "面向政务公开的公文语料构建",
    "authors": ["张三", "李四", "王五", "赵六"],
    "venue": "数据工程",
    "year": 2021,
    "issue": 2,
    "pages": "12-20",
}
EN_PREPRINT = {
    "type": "preprint",
    "title": "Sparse attention for long document understanding",
    "authors": ["Ava Stone", "Ben Fox"],
    "year": 2023,
    "arxiv_id": "2305.01234",
}
ZH_PREPRINT = {
    "type": "preprint",
    "title": "大语言模型的幻觉抑制方法",
    "authors": ["刘涛", "陈静"],
    "year": 2024,
    "url": "https://arxiv.org/abs/2403.09876",
}


def _formatted(tc: TestClient, pid: str, fmt: str) -> list[dict]:
    resp = tc.get(f"/api/v1/writing/projects/{pid}/citations", params={"format": fmt})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_format_gbt7714_mixed(client) -> None:
    tc, _ = client
    proj = _mk_project(tc)
    for i, meta in enumerate(
        [EN_JOURNAL, ZH_JOURNAL, ZH_JOURNAL_MANY, EN_PREPRINT], start=1
    ):
        _cite(tc, proj["id"], f"W{i}", meta)
    items = _formatted(tc, proj["id"], "gbt7714")
    assert items[0]["text"] == (
        "Shen Z, Cui B, Long M, et al. Retrieval-augmented generation for "
        "knowledge-intensive tasks[J]. Expert Systems, 2022, 39(1): e12770. "
        "DOI:10.1111/exsy.12770."
    )
    assert items[0]["degraded"] is False
    assert items[1]["text"] == (
        "王晓芳, 李明. 基于深度学习的中文分词方法研究[J]. 中文信息学报, 2020, 32(4): 45-52."
    )
    assert items[2]["text"] == (
        "张三, 李四, 王五, 等. 面向政务公开的公文语料构建[J]. 数据工程, 2021(2): 12-20."
    )
    assert items[3]["text"] == (
        f"Stone A, Fox B. Sparse attention for long document understanding[EB/OL]. "
        f"(2023)[{writing._today()}]. https://arxiv.org/abs/2305.01234."
    )


def test_format_apa_mixed(client) -> None:
    tc, _ = client
    proj = _mk_project(tc)
    for i, meta in enumerate(
        [EN_JOURNAL, ZH_JOURNAL, EN_PREPRINT, ZH_PREPRINT], start=1
    ):
        _cite(tc, proj["id"], f"W{i}", meta)
    items = _formatted(tc, proj["id"], "apa")
    assert items[0]["text"] == (
        "Shen, Z., Cui, B., Long, M., & Jiang, D. (2022). Retrieval-augmented "
        "generation for knowledge-intensive tasks. Expert Systems, 39(1), e12770. "
        "https://doi.org/10.1111/exsy.12770"
    )
    assert items[1]["text"] == (
        "王晓芳, & 李明. (2020). 基于深度学习的中文分词方法研究. 中文信息学报, 32(4), 45-52."
    )
    assert items[2]["text"] == (
        "Stone, A., & Fox, B. (2023). Sparse attention for long document "
        "understanding. arXiv. https://arxiv.org/abs/2305.01234"
    )
    assert items[3]["text"] == (
        "刘涛, & 陈静. (2024). 大语言模型的幻觉抑制方法. arXiv. "
        "https://arxiv.org/abs/2403.09876"
    )


def test_format_degraded_graceful(client) -> None:
    tc, _ = client
    proj = _mk_project(tc)
    _cite(
        tc,
        proj["id"],
        "B1",
        {"type": "book", "title": "Programming languages", "authors": ["Grace Hopper"], "year": 1953},
    )
    gbt = _formatted(tc, proj["id"], "gbt7714")[0]
    assert gbt["degraded"] is True
    assert "not fully covered by GB/T 7714" in gbt["text"]
    assert gbt["text"].startswith("Hopper G. Programming languages[Z]. 1953.")
    apa = _formatted(tc, proj["id"], "apa")[0]
    assert apa["degraded"] is True
    assert "not fully covered by APA 7" in apa["text"]
    # 字段残缺也不崩：无作者无年份
    _cite(tc, proj["id"], "X1", {"title": "Untitled fragment"})
    frag = _formatted(tc, proj["id"], "gbt7714")[1]
    assert frag["degraded"] is True and "[Z]" in frag["text"]


# ---- 4. 提纲：5 条引用 → ≥3 节，每节有依据或 needs_evidence ----

OUTLINE_CITES = [
    ("Retrieval augmented generation for open domain question answering", 2020),
    ("Retrieval augmented generation with knowledge graphs for multi hop reasoning", 2021),
    ("Large language models for few shot learning", 2022),
    ("Scaling laws for neural language models", 2020),
    ("Diffusion models for image synthesis", 2023),
]


def test_outline_deterministic_five_citations(client) -> None:
    tc, _ = client
    proj = _mk_project(tc)
    ids = [
        _cite(tc, proj["id"], f"W{i}", {"title": t, "year": y, "type": "article"})["id"]
        for i, (t, y) in enumerate(OUTLINE_CITES, start=1)
    ]
    out = tc.post(f"/api/v1/writing/projects/{proj['id']}/outline", json={"use_llm": False}).json()
    assert out["engine"] == "deterministic"
    assert out["citation_count"] == 5
    sections = out["sections"]
    assert len(sections) >= 3
    for sec in sections:
        assert sec["needs_evidence"] or sec["citation_ids"], sec
        for cid in sec["citation_ids"]:
            assert cid in ids
    # 结构性章节带待补标
    assert sections[0]["needs_evidence"] is True
    assert sections[-1]["needs_evidence"] is True
    # 依据章节：每节 basis 与 citation_ids 一一对应
    evidence = [s for s in sections if not s["needs_evidence"]]
    assert evidence and all(
        [b["citation_id"] for b in s["basis"]] == s["citation_ids"] for s in evidence
    )
    # 5 条引用全部被锚定（证据宁全勿丢）
    anchored = {cid for s in evidence for cid in s["citation_ids"]}
    assert anchored == set(ids)


def test_outline_llm_skipped_without_key(client) -> None:
    tc, _ = client
    proj = _mk_project(tc)
    _cite(tc, proj["id"], "W1", {"title": "A study of something", "year": 2023})
    out = tc.post(f"/api/v1/writing/projects/{proj['id']}/outline", json={"use_llm": True}).json()
    assert out["engine"] == "deterministic"
    assert out["llm"]["used"] is False and "FD_SHILUN_LLM_API_KEY" in out["llm"]["note"]
    # 无收藏引用 → 仅结构性骨架，全部待补
    proj2 = _mk_project(tc, title="空项目")
    out2 = tc.post(f"/api/v1/writing/projects/{proj2['id']}/outline", json={}).json()
    assert out2["citation_count"] == 0
    assert all(s["needs_evidence"] for s in out2["sections"])


# ---- 5. 引用可解校验：2 真 1 假 → 假被剥除，valid=2 ----


def test_validate_strips_unresolvable_citations(client) -> None:
    tc, _ = client
    proj = _mk_project(tc)
    c1 = _cite(tc, proj["id"], "W111", {"title": "Real one", "year": 2023})["id"]
    c2 = _cite(tc, proj["id"], "W222", {"title": "Real two", "year": 2024})["id"]
    text = (
        f"开篇依据 [[cite:{c1}]]。中段综合 [[cite:{c2}]] 与伪引用 [[cite:deadbeef99]]，"
        f"再引一次 [[cite:{c1}]]，还有畸形标记 [[cite:不存在的id]]。"
    )
    out = tc.post(f"/api/v1/writing/projects/{proj['id']}/validate", json={"text": text}).json()
    assert out["source"] == "body"
    assert out["valid"] == [c1, c2]
    assert out["stripped"] == ["deadbeef99", "不存在的id"]
    assert out["marker_count"] == 5
    assert f"[[cite:{c1}]]" in out["cleaned_text"]
    assert f"[[cite:{c2}]]" in out["cleaned_text"]
    assert "deadbeef99" not in out["cleaned_text"]
    assert "不存在的id" not in out["cleaned_text"]

    # 版本快照路径：无 body.text 时取最近版本快照
    tc.post(
        f"/api/v1/writing/projects/{proj['id']}/versions",
        json={"snapshot": {"text": f"快照正文 [[cite:{c1}]] 与 [[cite:ghost000000]]。"}},
    )
    out2 = tc.post(f"/api/v1/writing/projects/{proj['id']}/validate", json={}).json()
    assert out2["source"] == "latest_version" and out2["version_id"]
    assert out2["valid"] == [c1] and out2["stripped"] == ["ghost000000"]
    assert "[[cite:ghost000000]]" not in out2["cleaned_text"]


def test_validate_no_text_no_version_400(client) -> None:
    tc, _ = client
    proj = _mk_project(tc)
    resp = tc.post(f"/api/v1/writing/projects/{proj['id']}/validate", json={})
    assert resp.status_code == 400
