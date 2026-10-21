"""gov 面离线测试（shilun-serious-writing task 3.1/3.2/3.3）。

全离线：fixture 内嵌 5 篇小语料（通知 3 + 函 2），不连库、不联网、不碰
data/gov_corpus.json（FD_SHILUN_GOV_CORPUS_PATH 指向临时文件）。

auth.personal 由兄弟任务交付，此处可能尚未存在——用进程内替身顶住
（若真实模块已交付则直接用真实模块）。
"""
from __future__ import annotations

import json
import os
import sys
import types

# --- fd_shilun_api.personal 替身（auth.py 依赖它；离线测试不落任何审计盘） ----
try:  # pragma: no cover - 真实模块已交付时走这条路
    import fd_shilun_api.personal  # noqa: F401
except Exception:
    import fd_shilun_api  # noqa: E402

    _stub = types.ModuleType("fd_shilun_api.personal")

    class _Conn:
        def execute(self, *a, **k):  # noqa: ANN001
            return None

        def commit(self) -> None:
            return None

        def close(self) -> None:
            return None

    _stub.connect = lambda *a, **k: _Conn()
    _stub.migrate = lambda conn: None
    _stub.audit = lambda conn, user_id, action, path, status, detail=None: None
    sys.modules["fd_shilun_api.personal"] = _stub
    fd_shilun_api.personal = _stub

os.environ.setdefault("FD_SHILUN_AUTH_DEV_TOKENS", "test-token=user_a")
os.environ.pop("FD_SHILUN_LLM_API_KEY", None)  # 离线：LLM 润色必须走「跳过」分支

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fd_shilun_api import gov  # noqa: E402

# --- fixture 语料：5 篇两类文种 ---------------------------------------------


def _notice(doc_id: str, title: str, *, rich: bool, closing: str = "特此通知。") -> dict:
    body_tail = f"{closing}\n自然资源部\n2026年9月29日"
    if rich:
        sections = [
            {"section_path": "", "text": f"{title}\n各省、自治区、直辖市自然资源主管部门，新疆生产建设兵团自然资源局：\n为了规范矿产资源管理，根据《中华人民共和国矿产资源法》，现将有关事项通知如下："},
            {"section_path": "一、", "text": f"一、加强矿产资源保护\n各单位要落实保护责任。\n（一）落实巡查制度\n定期开展巡查。\n{body_tail}"},
        ]
    else:
        sections = [
            {"section_path": "", "text": f"{title}\n各省、自治区、直辖市自然资源主管部门：\n为了做好年度工作，现将有关事项通知如下：\n一、抓好贯彻落实\n请认真组织实施。\n{body_tail}"},
        ]
    return {
        "doc_id": doc_id,
        "source": "mnr",
        "source_url": f"https://example.gov.cn/{doc_id}.html",
        "title": title,
        "doc_number": "自然资发〔2026〕1号",
        "issuer": "自然资源部",
        "issuer_raw": "自然资源部",
        "doc_type": "通知",
        "date": "2026-09-29",
        "addressee": None,
        "sections": sections,
        "cited_docs": ["自然资发〔2020〕12号"],
    }


def _letter(doc_id: str, title: str) -> dict:
    return {
        "doc_id": doc_id,
        "source": "mof",
        "source_url": f"https://example.gov.cn/{doc_id}.html",
        "title": title,
        "doc_number": "财办会〔2026〕21号",
        "issuer": "财政部",
        "issuer_raw": "财政部办公厅",
        "doc_type": "函",
        "date": "2026-10-10",
        "addressee": None,
        "sections": [
            {
                "section_path": "",
                "text": (
                    f"{title}\n各省、自治区、直辖市财政厅（局）：\n"
                    "为了完善会计准则体系，现就征求意见有关事项函告如下：\n"
                    "一、征求意见的内容\n详见附件。\n特此函告。\n财政部办公厅\n2026年10月10日"
                ),
            }
        ],
        "cited_docs": [],
    }


FIXTURE_DOCS = [
    _notice("gov:mnr:n1", "自然资源部关于加强矿产资源保护工作的通知", rich=True),
    _notice("gov:mnr:n2", "自然资源部办公厅关于开展2026年度检查工作的通知", rich=False),
    _notice("gov:mnr:n3", "自然资源部关于通报表扬典型案例的通知", rich=False, closing="特此通知。"),
    _letter("gov:mof:l1", "财政部办公厅关于征求《政府会计准则制度解释第8号（征求意见稿）》意见的函"),
    _letter("gov:mof:l2", "国家发展改革委办公厅关于修订省间电力现货交易规则的复函"),
]


@pytest.fixture(scope="module")
def corpus_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("gov") / "gov_corpus.json"
    path.write_text(
        json.dumps(
            {
                "extracted_at": "2026-10-21T00:00:00+00:00",
                "source": {"kind": "fixture", "read_only": True},
                "doc_count": len(FIXTURE_DOCS),
                "docs": FIXTURE_DOCS,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="module")
def client(corpus_file):
    os.environ["FD_SHILUN_GOV_CORPUS_PATH"] = str(corpus_file)
    gov._corpus_cache = None
    gov._stats_cache = None
    app = FastAPI()
    app.include_router(gov.router)
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token"}


# --- 分类规则（rule-v1） ------------------------------------------------------


def test_classify_rules() -> None:
    f = gov.classify_doc_type
    assert f("自然资源部关于加强矿产资源保护工作的通知") == "通知"
    assert f("XX部关于设立新机构的请示") == "请示"
    assert f("关于修订省间电力现货交易规则的复函") == "函"
    assert f("财政部关于加快推进会计数智化工作的意见") == "意见"
    assert f("财政部令第110号--政府采购框架协议采购方式管理暂行办法") == "命令"  # 部令形态优先
    assert f("自然资源部所属单位2026年度公开招聘拟聘人员公示（一）") == "公示"  # 剥批次括注
    assert f("自然资源部2025年度部门决算") == "unknown"
    assert f(None) == "unknown"


# --- GET /doctypes -----------------------------------------------------------


def test_doctypes_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/gov/doctypes").status_code == 401


def test_doctypes_stats(client: TestClient) -> None:
    resp = client.get("/api/v1/gov/doctypes", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    by = {d["doctype"]: d for d in body["doctypes"]}
    assert by["通知"]["count"] == 3
    assert by["函"]["count"] == 2
    assert by["通知"]["sample_insufficient"] is False
    assert len(by["通知"]["sample_doc_ids"]) == 3
    # 与原库标注一致率（fixture 里两者对齐，应 100%）
    agree = body["agreement_vs_source_label"]
    assert agree["labeled"] == 5 and agree["rate"] == 1.0


# --- GET /template -----------------------------------------------------------


def test_template_notice_traceable(client: TestClient) -> None:
    resp = client.get("/api/v1/gov/template", params={"doctype": "通知"}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["doctype"] == "通知"
    assert body["sample_count"] == 3 and body["sample_insufficient"] is False
    assert len(body["sample_doc_ids"]) >= 3  # spec：可溯源 ≥3 条真实样例
    elements = [s["element"] for s in body["structure"]]
    assert elements == ["标题", "文号", "主送机关", "正文·前言", "正文·主体层级", "结语用语", "落款"]
    # 样例条目均可点开验证（doc_id 可解析回语料）
    for doc_id in body["sample_doc_ids"]:
        assert any(d["doc_id"] == doc_id for d in FIXTURE_DOCS)
    # 结语从语料实证归纳：fixture 三篇通知均「特此通知。」
    closing = body["structure"][5]
    assert closing["evidence"]["observed"][0]["phrase"] == "特此通知"
    # 主送机关出现率：fixture 3/3
    addr = body["structure"][2]["evidence"]
    assert addr["addressee_rate"] == 1.0
    assert body["constraints"]["称谓"]["observed_forms"]
    assert body["version"].startswith("gov-tpl-v1:")


def test_template_insufficient_doctype(client: TestClient) -> None:
    resp = client.get("/api/v1/gov/template", params={"doctype": "请示"}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sample_count"] == 0
    assert body["sample_insufficient"] is True
    assert "仅结构参考" in body["note"]
    assert body["sample_doc_ids"] == []


def test_template_unknown_doctype_422(client: TestClient) -> None:
    resp = client.get("/api/v1/gov/template", params={"doctype": "诗歌"}, headers=AUTH)
    assert resp.status_code == 422


# --- GET /samples/{doc_id}：引用可点开验证 ------------------------------------


def test_sample_lookup(client: TestClient) -> None:
    doc_id = FIXTURE_DOCS[0]["doc_id"]
    resp = client.get(f"/api/v1/gov/samples/{doc_id}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_id"] == doc_id
    assert body["title"] == FIXTURE_DOCS[0]["title"]
    assert body["source_url"].startswith("https://")
    assert body["sections"] and body["sections"][0]["section_path"] == ""


def test_sample_lookup_404(client: TestClient) -> None:
    resp = client.get("/api/v1/gov/samples/gov:mnr:nope", headers=AUTH)
    assert resp.status_code == 404


# --- POST /draft --------------------------------------------------------------


def test_draft_notice_deterministic(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/gov/draft",
        headers=AUTH,
        json={
            "doctype": "通知",
            "subject": "开展2026年度矿产资源保护监督检查",
            "key_points": ["明确检查范围与重点", "落实整改时限", "强化结果运用"],
            "issuer": "自然资源部",
            "addressee": "各省、自治区、直辖市自然资源主管部门",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "自然资源部关于开展2026年度矿产资源保护监督检查的通知"
    by_elem = {s["element"]: s for s in body["sections"]}
    assert by_elem["主送机关"]["text"].endswith("：")
    assert "一、明确检查范围与重点" in by_elem["正文·主体"]["text"]
    assert by_elem["结语"]["text"].startswith("特此通知")
    assert by_elem["落款"]["text"].startswith("自然资源部")
    # 溯源：所用模板与样例 doc_id 齐备，且每条引用都能解析回语料
    assert body["template"]["sample_doc_ids"] and len(body["template"]["sample_doc_ids"]) >= 3
    assert body["template"]["sample_insufficient"] is False
    corpus_ids = {d["doc_id"] for d in FIXTURE_DOCS}
    for cit in body["citations"]:
        assert cit["doc_id"] in corpus_ids
        assert cit["source_url"].startswith("https://")
    # 离线：无 LLM KEY → 未润色且如实说明
    assert body["polished"] is False
    assert "FD_SHILUN_LLM_API_KEY" in body["polish_note"]
    assert body["warnings"] == []


def test_draft_qingshi_flags_insufficient(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/gov/draft",
        headers=AUTH,
        json={"doctype": "请示", "subject": "增设矿产资源保护专项经费", "key_points": ["经费规模与用途"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["doctype"] == "请示"
    assert body["title"].endswith("的请示")
    assert body["template"]["sample_insufficient"] is True
    assert any("样例不足" in w for w in body["warnings"])
    by_elem = {s["element"]: s for s in body["sections"]}
    assert "请示如下" in by_elem["正文·前言"]["text"]
    assert "妥否，请批示" in by_elem["结语"]["text"]


def test_draft_unknown_doctype_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/gov/draft", headers=AUTH, json={"doctype": "散文", "subject": "随便写写"}
    )
    assert resp.status_code == 422


def test_draft_missing_subject_422(client: TestClient) -> None:
    resp = client.post("/api/v1/gov/draft", headers=AUTH, json={"doctype": "通知"})
    assert resp.status_code == 422


# --- 语料缺失即诚实 503 -------------------------------------------------------


def test_corpus_missing_is_503(tmp_path) -> None:
    os.environ["FD_SHILUN_GOV_CORPUS_PATH"] = str(tmp_path / "nope.json")
    gov._corpus_cache = None
    gov._stats_cache = None
    app = FastAPI()
    app.include_router(gov.router)
    with TestClient(app) as c:
        resp = c.get("/api/v1/gov/doctypes", headers=AUTH)
        assert resp.status_code == 503
        assert "语料不可用" in resp.json()["detail"]
