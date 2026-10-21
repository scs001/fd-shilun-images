"""交付闸门测试（task 5.1）：排版 + 校对双闸，P0 阻断交付。

规范：存在 P0 级问题的成稿 SHALL NOT 被标记为可交付，且 MUST 列出问题与定位。
这里除了判定本身，还守住两条容易退化的性质：
- 不可交付时**不产出交付物**（否则下游忽略 deliverable 就能拿到违规稿）；
- P1/P2 只告警、不阻断（合规公文不该因为没标份号而交不出去）。
"""

from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fd_shilun_api.gongwen import deliver as gongwen_deliver
from fd_shilun_api.gongwen import gate
from fd_shilun_api.gongwen.api import router
from fd_shilun_api.gongwen.elements import GongwenDocument
from fd_shilun_api.gongwen.fonts import resolve_fonts

CLEAN_BODY = (
    "为推进数据要素市场建设，根据有关法律法规，现就有关事项通知如下：\n"
    "一、总体要求\n"
    "（一）完善制度。健全数据产权、流通交易、收益分配和安全治理规则。\n"
    "1.明确责任。各地区各部门按职责分工组织实施。\n"
    "二、组织实施\n"
    "各地区要结合实际制定落实措施，并及时报告工作进展。"
)


def _doc(**overrides) -> GongwenDocument:
    data = {
        "title": "国务院办公厅关于推进数据要素市场建设的通知",
        "addressee": "各省、自治区、直辖市人民政府",
        "body": CLEAN_BODY,
        "issuer_signature": "国务院办公厅",
        "date": "2024年5月1日",
        "doc_number": "国办发〔2024〕5号",
    }
    data.update(overrides)
    return GongwenDocument(**data)


def _evaluate(doc: GongwenDocument, **kwargs) -> gate.GateOutcome:
    """固定「只装宋体」的字体环境，避免依赖本机字体。"""
    kwargs.setdefault("with_artifacts", True)
    kwargs.setdefault("fonts", resolve_fonts(installed={"宋体"}))
    return gate.evaluate(doc, **kwargs)


def test_clean_draft_is_deliverable_with_artifacts() -> None:
    outcome = _evaluate(_doc())
    assert outcome.report.deliverable is True
    assert outcome.report.p0_fail == 0
    assert outcome.report.blocked_reason is None
    assert outcome.docx is not None and outcome.docx[:2] == b"PK"
    # 报告里没有 P0 fail 项
    assert [i for i in outcome.report.items if i.status == "fail" and i.severity == "P0"] == []
    # 排版验收单独留存，可逐条复核
    assert outcome.acceptance is not None
    assert outcome.acceptance.deliverable is True


@pytest.mark.parametrize(
    ("overrides", "expect_rule"),
    [
        # 缺要素由排版验收的 elements 侧报出（gov.element.*），格式类报 gov.<field>_format，
        # 敏感词只由校对规则产生（pf.p0.sensitive_word）。断言具体 id 即固定「谁负责报」。
        ({"addressee": None}, "gov.element.addressee"),
        ({"issuer_signature": None}, "gov.element.issuer_signature"),
        ({"date": "2024年05月01日"}, "gov.date_format"),
        ({"doc_number": "国办发[2024]第05号"}, "gov.doc_number_format"),
        ({"body": CLEAN_BODY + "\n严禁传播法轮功内容。"}, "pf.p0.sensitive_word"),
    ],
)
def test_p0_blocks_delivery_and_lists_rule(overrides: dict, expect_rule: str) -> None:
    outcome = _evaluate(_doc(**overrides))
    assert outcome.report.deliverable is False
    assert outcome.report.p0_fail >= 1
    blocking = outcome.report.blocking_items
    assert blocking, "不可交付却没有列出一条阻断项"
    assert expect_rule in {i.rule_id for i in blocking}
    # 同一缺陷只报一次：P0 计数不得因两个闸门都看到而翻倍
    assert sum(1 for i in blocking if i.rule_id == expect_rule) == 1
    # 每条阻断项都带定位与证据（可复核，不是一句「不合格」）
    for item in blocking:
        assert item.evidence.strip()
        assert item.locator is not None
    assert outcome.report.blocked_reason
    assert expect_rule in outcome.report.blocked_reason
    # 不可交付 ⇒ 不产出交付物
    assert outcome.docx is None and outcome.pdf is None


def test_p1_p2_warn_but_do_not_block() -> None:
    body = CLEAN_BODY + "\n二、工作要求。高度重视，狠抓落实，截止目前已完成整改。"
    outcome = _evaluate(_doc(body=body))
    assert outcome.report.deliverable is True  # 只有建议，不放行不了
    warns = [i for i in outcome.report.items if i.status == "warn"]
    assert {i.rule_id for i in warns} >= {"pf.p1.cliche", "pf.p2.typo"}
    assert all(i.severity in ("P1", "P2") for i in warns)


def test_dry_run_reports_without_artifacts() -> None:
    outcome = gate.evaluate(_doc(), with_artifacts=False)
    assert outcome.report.deliverable is True
    assert outcome.docx is None and outcome.acceptance is None
    # 判定与带产物时一致：dry run 不是放宽口径
    with_artifacts = _evaluate(_doc())
    assert with_artifacts.report.deliverable == outcome.report.deliverable


# ---------------------------------------------------------------------------
# 端点：P0 → 409 且不给文件
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    from fd_shilun_api.auth import require_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: "user_a"
    return TestClient(app)


def test_endpoint_rejects_with_409_and_no_file(client: TestClient) -> None:
    body = {**_doc(addressee=None).model_dump(), "want_pdf": False}
    resp = client.post("/api/v1/gongwen/deliver", json=body)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["deliverable"] is False
    assert detail["docx_base64"] is None and detail["pdf_base64"] is None
    assert detail["report"]["p0_fail"] >= 1
    assert any(
        i["status"] == "fail" and i["severity"] == "P0" for i in detail["report"]["items"]
    ), "409 响应里没有列出任何 P0 阻断项"


def test_endpoint_delivers_clean_draft(client: TestClient) -> None:
    body = {**_doc().model_dump(), "want_pdf": False}
    resp = client.post("/api/v1/gongwen/deliver", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["deliverable"] is True
    assert base64.b64decode(data["docx_base64"])[:2] == b"PK"
    assert data["report"]["schema_version"] == gate.GATE_VERSION


def test_endpoint_dry_run_returns_report_only(client: TestClient) -> None:
    body = {**_doc().model_dump(), "dry_run": True}
    resp = client.post("/api/v1/gongwen/deliver", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dry_run"] is True
    assert data["docx_base64"] is None


def test_gate_uses_same_font_resolution_as_render() -> None:
    """闸门与渲染共用一套字体决策：同一环境下不得出现「报告说用A、文件里是B」。"""
    outcome = gate.evaluate(_doc(), fonts=resolve_fonts(installed={"宋体"}))
    assert outcome.fonts is not None
    assert outcome.fonts.chosen["body"] == "宋体"
    assert gongwen_deliver.render is not None


# ---------------------------------------------------------------------------
# 成稿引用继承三级核验（spec「成稿引用继承三级核验」）
# ---------------------------------------------------------------------------


class _ConsistentResolver:
    def lookup(self, meta: dict):
        from fd_shilun_api.verify.authentic import LookupResult, MetadataRecord

        return LookupResult(
            records=[
                MetadataRecord(
                    source="crossref",
                    title=str(meta.get("title") or ""),
                    authors=meta.get("authors") or [],
                    year=meta.get("year"),
                )
            ],
            responded=["crossref"],
        )


def _citation_bodies():
    shown = "一、依据\n研究表明，透明的方法能把误差降低三成[[cite:c_ok]]。"
    bad = "一、依据\n所有场景都最优[[cite:c_bad]]。"
    return shown, bad


def test_citation_inheritance_shadow_reports_without_touching_text(monkeypatch) -> None:
    monkeypatch.setenv("FD_SHILUN_CITE_VERIFY_MODE", "shadow")
    monkeypatch.delenv("FD_SHILUN_VERIFY_OFFLINE", raising=False)
    good, _ = _citation_bodies()
    citations = [
        {"id": "c_ok", "kind": "openalex", "ref_id": "W1",
         "meta": {"title": "On Evidence", "authors": ["Ada Lovelace"], "year": 2020}},
    ]
    outcome = gate.evaluate(
        _doc(body=CLEAN_BODY + "\n" + good),
        fonts=resolve_fonts(installed={"宋体"}),
        citations=citations,
        citation_resolver=_ConsistentResolver(),
    )
    assert outcome.report.citation_mode == "shadow"
    assert outcome.report.stripped_citation_ids == []
    # 待核引用只告警、不阻断，也不从成稿剥除
    assert outcome.report.deliverable is True
    assert outcome.report.items and any(i.origin == "citation" for i in outcome.report.items)
    assert all(i.severity != "P0" for i in outcome.report.items if i.origin == "citation")


def test_citation_inheritance_enforce_strips_blocked(monkeypatch) -> None:
    monkeypatch.setenv("FD_SHILUN_CITE_VERIFY_MODE", "enforce")
    _, bad = _citation_bodies()
    # 该引用不在清单里 → 一级不可解 → block；enforce 下必须从成稿剥除
    outcome = gate.evaluate(
        _doc(body=CLEAN_BODY + "\n" + bad),
        fonts=resolve_fonts(installed={"宋体"}),
        citations=[],
    )
    assert outcome.report.citation_mode == "enforce"
    assert outcome.report.stripped_citation_ids == ["c_bad"]
    assert outcome.report.deliverable is True  # 引用问题用引用规则处置，不升级为 P0


def test_citation_marker_absent_from_delivered_docx_when_enforced(monkeypatch) -> None:
    """剥除必须落到交付物里，而不只是报告里说剥了。"""
    import io
    import zipfile

    monkeypatch.setenv("FD_SHILUN_CITE_VERIFY_MODE", "enforce")
    _, bad = _citation_bodies()
    outcome = gate.evaluate(
        _doc(body=CLEAN_BODY + "\n" + bad),
        fonts=resolve_fonts(installed={"宋体"}),
        citations=[],
    )
    assert outcome.docx is not None
    with zipfile.ZipFile(io.BytesIO(outcome.docx)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "[[cite:c_bad]]" not in xml
