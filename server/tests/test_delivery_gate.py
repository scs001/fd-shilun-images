"""交付闸门集成测试（task 5.1）。

验证「排版 + 校对」两道闸门都接进了成稿交付流程：

- 存在 P0 校对问题时成稿不可标记为可交付；
- P1/P2 校对问题只进报告（warn），不阻断交付；
- 校对只读：渲染前后正文哈希一致（校对不得改稿）；
- 要素类 P0 不在报告里重复计数（elements 与校对表共用 format_check）；
- 校对路由与规则表路由可用，且 P0 不可被忽略。
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from fd_shilun_api import main
from fd_shilun_api.gongwen import deliver, elements, fonts

FULL_FIELDS = {
    "copy_number": "000001",
    "secrecy": "公开",
    "urgency": "特急",
    "issuer_mark": "某某市人民政府文件",
    "doc_number": "某政发〔2024〕5号",
    "signer": "张三",
    "title": "关于加强某项工作的意见",
    "addressee": "各县（市、区）人民政府",
    "body": (
        "为贯彻落实上级部署，现提出如下意见。\n"
        "一、总体要求\n"
        "坚持稳中求进，扎实推进各项工作。\n"
        "二、主要任务\n"
        "（一）健全工作机制。完善责任体系。\n"
        "（二）强化督促检查。确保落地见效。\n"
    ),
    "issuer_signature": "某某市人民政府",
    "date": "2024年5月1日",
    "printing_org": "某某市人民政府办公室",
    "printing_date": "2024年5月2日",
}


def _doc(**overrides) -> elements.GongwenDocument:
    return elements.GongwenDocument(**{**FULL_FIELDS, **overrides})


def _render(doc: elements.GongwenDocument | None = None) -> deliver.DeliveryResult:
    resolution = fonts.resolve_fonts(installed={"宋体"})
    return deliver.render(doc if doc is not None else _doc(), fonts=resolution)


def _by_rule(result: deliver.DeliveryResult) -> dict[str, object]:
    return {item.rule_id: item for item in result.report.items}


# ---------------------------------------------------------------------------
# 闸门生效
# ---------------------------------------------------------------------------


def test_clean_document_is_deliverable() -> None:
    result = _render()
    assert result.report.deliverable is True
    assert result.report.counts["p0_fail"] == 0


def test_p0_sensitive_word_blocks_delivery() -> None:
    """敏感词是校对表的 P0 规则：命中即不可交付。"""
    result = _render(_doc(body="为贯彻落实部署，现提出如下意见。\n坚决抵制法轮功。\n"))
    proofread_p0 = [
        item
        for item in result.report.items
        if item.rule_id.startswith("gov.proofread.") and item.severity == "P0"
    ]
    assert proofread_p0, "敏感词必须由校对闸门以 P0 报出"
    assert all(item.status == "fail" for item in proofread_p0)
    assert result.report.deliverable is False


def test_p1_proofread_issue_warns_but_does_not_block() -> None:
    """套话是 P1：进报告（warn），但不应让合规公文不可交付。"""
    result = _render(
        _doc(body="为贯彻落实部署，现提出如下意见。\n各单位要高度重视，狠抓落实。\n")
    )
    warnings = [
        item
        for item in result.report.items
        if item.rule_id.startswith("gov.proofread.") and item.severity == "P1"
    ]
    assert warnings, "套话应由校对闸门以 P1 报出"
    assert all(item.status == "warn" for item in warnings)
    assert result.report.deliverable is True


def test_p2_proofread_issue_warns_only() -> None:
    """连续标点是 P2：只告警。"""
    result = _render(_doc(body="为贯彻落实部署，现提出如下意见。。\n"))
    p2 = [
        item
        for item in result.report.items
        if item.rule_id.startswith("gov.proofread.") and item.severity == "P2"
    ]
    assert p2
    assert all(item.status == "warn" for item in p2)
    assert result.report.deliverable is True


def test_element_p0_not_double_counted() -> None:
    """发文字号格式错：elements 报一次 P0，校对表不再重复报同一个 P0。"""
    result = _render(_doc(doc_number="某政发[2024]第05号"))
    doc_number_items = [
        item
        for item in result.report.items
        if item.rule_id in ("gov.doc_number_format", "gov.proofread.pf.p0.doc_number_format")
    ]
    assert len(doc_number_items) == 1, "同一缺陷不得在报告里出现两次"
    assert doc_number_items[0].rule_id == "gov.doc_number_format"
    assert doc_number_items[0].severity == "P0"
    assert result.report.deliverable is False


def test_missing_element_p0_not_double_counted() -> None:
    """缺主送机关：elements 报一次，校对表不再重复。"""
    doc = elements.GongwenDocument(
        title="关于某项工作的通知",
        body="现将有关事项通知如下。\n",
        issuer_signature="某某市人民政府",
        date="2024年5月1日",
    )
    result = _render(doc)
    missing = [
        item
        for item in result.report.items
        if item.rule_id == "gov.proofread.pf.p0.missing_element"
    ]
    assert missing == [], "缺要素由 elements 统一报，校对表不重复"
    assert result.report.deliverable is False


def test_skip_proofread_only_for_internal_layout_checks() -> None:
    """skip_proofread 只跳过校对；版式与要素仍照常验收。"""
    resolution = fonts.resolve_fonts(installed={"宋体"})
    doc = _doc(body="为贯彻落实部署，现提出如下意见。\n坚决抵制法轮功。\n")
    result = deliver.render(doc, fonts=resolution, skip_proofread=True)
    assert not any(item.rule_id.startswith("gov.proofread.") for item in result.report.items)
    assert any(item.rule_id.startswith("gov.layout.") for item in result.report.items)


# ---------------------------------------------------------------------------
# 只读性
# ---------------------------------------------------------------------------


def test_proofread_gate_does_not_modify_body() -> None:
    """校对闸门只报告：渲染前后正文字节不变。"""
    doc = _doc(body="为贯彻落实部署，现提出如下意见。\n各单位要高度重视，狠抓落实。\n")
    before = hashlib.sha256(doc.body.encode("utf-8")).hexdigest()
    _render(doc)
    after = hashlib.sha256(doc.body.encode("utf-8")).hexdigest()
    assert before == after


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    """独立 app + 鉴权替身（与 test_writing 同配方，不污染 main.app）。"""
    from fastapi import FastAPI

    from fd_shilun_api.gongwen import api as gongwen_api
    from fd_shilun_api.proofread import api as proofread_api

    app = FastAPI()
    app.include_router(gongwen_api.router)
    app.include_router(proofread_api.router)
    app.dependency_overrides[gongwen_api.require_user] = lambda: "tester"
    app.dependency_overrides[proofread_api.require_user] = lambda: "tester"
    return TestClient(app)


def test_proofread_route_returns_issue_list(client: TestClient) -> None:
    response = client.post(
        "/api/v1/proofread",
        json={"text": "各单位要高度重视，狠抓落实。", "fields": None},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert "issues" in data and "counts" in data
    assert data["counts"]["total"] >= 1
    # 只读：响应里没有改写后的正文
    assert "text" not in data and "cleaned_text" not in data


def test_proofread_route_rejects_ignoring_p0(client: TestClient) -> None:
    response = client.post(
        "/api/v1/proofread",
        json={"text": "正文。", "fields": None, "ignore_rule_ids": ["pf.p0.sensitive_word"]},
    )
    assert response.status_code == 400
    assert "P0" in response.json()["detail"]


def test_rules_route_exposes_version(client: TestClient) -> None:
    response = client.get("/api/v1/proofread/rules")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["version"]
    assert data["rules"]
    assert all("severity" in rule for rule in data["rules"])


def test_gongwen_render_route_blocks_p0(client: TestClient) -> None:
    """成稿交付路由：P0 时 deliverable=false，但文件仍返回（可下载后修）。"""
    payload = {**FULL_FIELDS, "doc_number": "某政发[2024]第05号"}
    response = client.post("/api/v1/gongwen/render", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["deliverable"] is False
    assert data["docx_base64"]
    p0 = [i for i in data["report"]["items"] if i["status"] == "fail" and i["severity"] == "P0"]
    assert p0


def test_main_app_mounts_gongwen_and_proofread() -> None:
    """装配检查：两道闸门的路由确实挂在主应用上（task 5.1）。

    断言 openapi() 的路径表而不是 app.routes 的内部结构：Starlette 1.9 起
    include_router 不再把子路由摊平进 app.routes（改挂 _IncludedRouter 条目，
    没有 .path），按内部结构断言会在新版本上假红；openapi 才是对外契约面。
    """
    paths = set(main.app.openapi()["paths"])
    assert "/api/v1/gongwen/render" in paths
    assert "/api/v1/proofread" in paths
    assert "/api/v1/proofread/rules" in paths
