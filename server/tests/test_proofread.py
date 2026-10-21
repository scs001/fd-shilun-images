"""公文校对离线单测（tasks 4.1-4.4）。

覆盖四件事：
1. 规则表：带版本号、id 唯一、可增删；坏规则（重复 id / 非法级别 / 坏正则 /
   缺依据）在加载期就带 id 报错。
2. P0：必需要素缺失、发文字号、成文日期、敏感词；P0 不可忽略，P1/P2 可忽略留痕。
3. P1/P2：每个类别都有样例命中，且字符区间能切回命中文本。
4. 只读：执行前后正文哈希一致，结果对象不携带改写文本；干净公文不误报。

全离线：只用包内规则表与 format_check 的确定性口径，不连库、不联网。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from fd_shilun_api.proofread import (
    P0NotIgnorable,
    RuleTableError,
    load_rules,
    proofread,
)

# ---------------------------------------------------------------------------
# 公共样例
# ---------------------------------------------------------------------------


def _full_fields(**overrides: str | None) -> dict[str, str | None]:
    """一份要素齐全的成稿字段；用 overrides 替换/置空来构造缺失场景。"""
    fields: dict[str, str | None] = {
        "title": "某部关于规范矿产资源管理的通知",
        "addressee": "各省、自治区、直辖市自然资源主管部门：",
        "issuer_signature": "某部",
        "date": "2024年5月1日",
        "doc_number": "国发〔2024〕5号",
    }
    fields.update(overrides)
    return fields


def _extra_rule(**overrides: object) -> dict:
    """默认合法的一条 P1 扩充规则，按需覆盖字段以构造坏规则。"""
    rule: dict = {
        "id": "pf.p1.extra_test",
        "severity": "P1",
        "category": "套话",
        "match": {"kind": "literal", "patterns": ["测试套话"]},
        "message": "测试规则命中",
        "suggestion": "删除测试措辞",
        "basis": "测试用规则（离线用例）",
    }
    rule.update(overrides)
    return rule


def _issues(result, rule_id: str) -> list:
    return [i for i in result.issues if i.rule_id == rule_id]


CLEAN_DOC = (
    "为规范矿产资源管理，根据《中华人民共和国矿产资源法》，现将有关事项通知如下：\n"
    "一、明确管理职责\n"
    "各单位应当按照职责分工做好矿产资源保护工作。\n"
    "（一）落实巡查制度\n"
    "定期开展矿区巡查，发现问题及时整改。\n"
    "二、加强监督检查\n"
    "每年开展一次专项检查，检查结果向社会公布。\n"
    "特此通知。"
)


# ---------------------------------------------------------------------------
# 4.1 规则表结构与加载器
# ---------------------------------------------------------------------------


def test_default_table_has_version_and_well_formed_rules() -> None:
    rs = load_rules()
    assert rs.version == "proofread-v1"
    assert rs.rules

    ids = [r.id for r in rs.rules]
    assert len(ids) == len(set(ids))

    by_category: dict[str, set[str]] = {}
    for rule in rs.rules:
        assert rule.severity in {"P0", "P1", "P2"}
        assert rule.category.strip()
        assert rule.message.strip()
        assert rule.basis.strip(), f"{rule.id} 缺依据"
        if rule.match["kind"] != "required_field":
            assert rule.suggestion, f"{rule.id} 缺修改建议"
        by_category.setdefault(rule.category, set()).add(rule.severity)

    # spec 要求覆盖的类别一个都不能少
    required_categories = {
        "套话", "军事化表达", "回避式表态", "占位符", "连续标点",
        "半角标点", "层次序数", "成文日期", "错别字", "敏感词",
    }
    assert required_categories <= set(by_category)
    # 同一类别不允许跨级别（级别是规则的属性，不是匹配结果的属性）
    assert all(len(levels) == 1 for levels in by_category.values())


def test_default_table_loads_independent_of_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_rules().version == "proofread-v1"


def test_add_rule_via_extra() -> None:
    rs = load_rules(extra=[_extra_rule()])
    assert "pf.p1.extra_test" in rs.by_id
    res = proofread("这里是一段测试套话。", ruleset=rs)
    assert [i.rule_id for i in res.issues] == ["pf.p1.extra_test"]
    assert res.issues[0].severity == "P1"


def test_remove_rule_via_removed() -> None:
    rs = load_rules(removed=["pf.p1.cliche"])
    assert "pf.p1.cliche" not in rs.by_id
    res = proofread("要高度重视这项工作。", ruleset=rs)
    assert not _issues(res, "pf.p1.cliche")
    # 默认表仍然保留该规则（removed 只影响本次加载）
    assert _issues(proofread("要高度重视这项工作。"), "pf.p1.cliche")


def test_ruleset_with_rules_and_without_helpers() -> None:
    rs = load_rules()
    trimmed = rs.without(["pf.p2.typo"])
    assert trimmed.version == rs.version
    assert "pf.p2.typo" not in trimmed.by_id
    assert len(trimmed.rules) == len(rs.rules) - 1

    extended = trimmed.with_rules(_extra_rule())
    assert "pf.p1.extra_test" in extended.by_id
    assert "pf.p2.typo" not in extended.by_id

    # 增补重复 id 不静默覆盖
    with pytest.raises(RuleTableError) as excinfo:
        rs.with_rules(_extra_rule(id="pf.p1.cliche"))
    assert excinfo.value.rule_id == "pf.p1.cliche"

    # 移除未知 id 视为拼写错误，直接报错
    with pytest.raises(RuleTableError) as excinfo:
        rs.without(["pf.not_exist"])
    assert excinfo.value.rule_id == "pf.not_exist"


@pytest.mark.parametrize(
    ("bad_rule", "expected_id"),
    [
        pytest.param(_extra_rule(id="pf.p1.cliche"), "pf.p1.cliche", id="duplicate-id"),
        pytest.param(
            _extra_rule(id="pf.p1.bad_sev", severity="P3"), "pf.p1.bad_sev", id="bad-severity"
        ),
        pytest.param(
            _extra_rule(
                id="pf.p1.bad_regex",
                match={"kind": "regex", "patterns": ["(未闭合"]},
            ),
            "pf.p1.bad_regex",
            id="bad-regex",
        ),
        pytest.param(
            _extra_rule(id="pf.p1.no_basis", basis=""), "pf.p1.no_basis", id="missing-basis"
        ),
        pytest.param(
            _extra_rule(id="pf.p1.no_suggestion", suggestion=""),
            "pf.p1.no_suggestion",
            id="missing-suggestion",
        ),
        pytest.param(
            _extra_rule(id="pf.p1.bad_kind", match={"kind": "llm_judge"}),
            "pf.p1.bad_kind",
            id="unknown-match-kind",
        ),
    ],
)
def test_bad_rule_rejected_with_offending_id(bad_rule: dict, expected_id: str) -> None:
    with pytest.raises(RuleTableError) as excinfo:
        load_rules(extra=[bad_rule])
    assert excinfo.value.rule_id == expected_id
    assert expected_id in str(excinfo.value)


def test_table_without_version_rejected(tmp_path) -> None:
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps({"rules": [_extra_rule()]}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(RuleTableError) as excinfo:
        load_rules(path)
    assert "version" in str(excinfo.value)


def test_missing_table_file_rejected(tmp_path) -> None:
    with pytest.raises(RuleTableError):
        load_rules(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# 4.2 P0：确定性判定，不可忽略
# ---------------------------------------------------------------------------


def test_missing_required_elements_are_three_p0() -> None:
    # 只给标题与文号：主送机关、发文机关署名、成文日期三项缺失
    res = proofread(
        "现将有关事项通知如下。",
        fields={"title": "某部关于规范管理的通知", "doc_number": "国发〔2024〕5号"},
    )
    missing = _issues(res, "pf.p0.missing_element")
    assert len(missing) == 3
    assert {i.evidence for i in missing} == {"主送机关", "发文机关署名", "成文日期"}
    assert all(i.severity == "P0" for i in missing)
    # 缺失要素必须机读到字段（field）：否则调用方只能从证据文本里猜是哪个要素
    assert all(i.locator.kind == "field" and i.locator.field for i in missing)
    assert {i.locator.field for i in missing} == {"addressee", "issuer_signature", "date"}
    assert res.counts == {"P0": 3, "P1": 0, "P2": 0, "total": 3}
    assert res.blocking is True
    assert len(res.p0_issues) == 3


def test_doc_number_format_is_p0_with_corrected_suggestion() -> None:
    res = proofread("正文。", fields=_full_fields(doc_number="国发[2024]第05号"))
    hits = _issues(res, "pf.p0.doc_number_format")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.severity == "P0"
    assert hit.suggestion == "国发〔2024〕5号"  # 报错要能直接照抄
    assert hit.locator.kind == "field"
    assert hit.locator.field == "doc_number"
    assert hit.evidence == "国发[2024]第05号"


def test_date_format_is_p0_with_corrected_suggestion() -> None:
    res = proofread("正文。", fields=_full_fields(date="2024年05月01日"))
    hits = _issues(res, "pf.p0.date_format")
    assert len(hits) == 1
    assert hits[0].severity == "P0"
    assert hits[0].suggestion == "2024年5月1日"
    assert hits[0].locator.kind == "field"
    assert hits[0].locator.field == "date"


def test_sensitive_word_is_p0_with_text_locator() -> None:
    text = "坚决反对台独分裂行径。"
    res = proofread(text)
    hits = _issues(res, "pf.p0.sensitive_word")
    assert len(hits) == 1
    assert hits[0].severity == "P0"
    assert text[hits[0].locator.char_start : hits[0].locator.char_end] == "台独"
    assert res.issues[0].rule_id == "pf.p0.sensitive_word"


@pytest.mark.parametrize(
    "rule_id", [r.id for r in load_rules().severities["P0"]]
)
def test_p0_rule_cannot_be_ignored(rule_id: str) -> None:
    with pytest.raises(P0NotIgnorable) as excinfo:
        proofread("正文。", ignored_rule_ids=[rule_id])
    assert excinfo.value.rule_id == rule_id


def test_p1_p2_are_ignorable_and_recorded() -> None:
    res = proofread(
        "要高度重视。特此通知。。",
        ignored_rule_ids=["pf.p1.cliche", "pf.p2.punct_repeat"],
    )
    assert res.ignored == ["pf.p1.cliche", "pf.p2.punct_repeat"]
    assert not _issues(res, "pf.p1.cliche")
    assert not _issues(res, "pf.p2.punct_repeat")


def test_structural_rules_need_fields() -> None:
    # fields=None 表示只校对正文、不掌握要素清单：不做莫须有的缺项 P0 判定
    assert not _issues(proofread("现将有关事项通知如下。"), "pf.p0.missing_element")
    # 显式给空要素清单 = 要素确实为空，如实报缺失
    assert _issues(proofread("现将有关事项通知如下。", fields={}), "pf.p0.missing_element")


# ---------------------------------------------------------------------------
# 4.3 P1/P2：每类样例命中并给出可切回的定位
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_id", "text", "expected"),
    [
        ("pf.p1.cliche", "我们要高度重视这项工作。", "高度重视"),
        ("pf.p1.militarized", "以挂图作战方式推进。", "挂图作战"),
        ("pf.p1.evasive", "该事项尚需进一步研究。", "尚需进一步研究"),
        ("pf.p1.placeholder", "主送机关：〔主送机关全称〕。", "〔主送机关全称〕"),
        ("pf.p2.punct_repeat", "特此通知。。", "。。"),
        ("pf.p1.punct_halfwidth", "2024年增长10.5%,居首位。", ","),
        ("pf.p2.typo", "请按此布署落实。", "布署"),
    ],
)
def test_each_category_hits_with_exact_locator(
    rule_id: str, text: str, expected: str
) -> None:
    res = proofread(text)
    hits = _issues(res, rule_id)
    assert hits, f"{rule_id} 未命中样例：{text!r}"
    loc = hits[0].locator
    assert loc.kind == "char_range"
    assert text[loc.char_start : loc.char_end] == expected
    assert hits[0].evidence == expected
    assert loc.paragraph_index == text.count("\n", 0, loc.char_start)


def test_typo_suggestion_names_correct_form() -> None:
    hit = _issues(proofread("请按此布署落实。"), "pf.p2.typo")[0]
    assert "部署" in hit.suggestion


def test_hierarchy_skip_after_level_one() -> None:
    text = "一、总体要求\n1.加强组织领导\n（一）明确责任\n"
    hits = _issues(proofread(text), "pf.p1.hierarchy_skip")
    assert len(hits) == 1
    loc = hits[0].locator
    assert text[loc.char_start : loc.char_end] == "1."
    assert loc.paragraph_index == 1
    assert "（一）" in hits[0].suggestion


def test_hierarchy_wrong_digit_marker() -> None:
    text = "一、总体要求\n1、加强领导\n"
    hits = _issues(proofread(text), "pf.p1.hierarchy_skip")
    assert len(hits) == 1
    assert text[hits[0].locator.char_start : hits[0].locator.char_end] == "1、"
    assert "1." in hits[0].suggestion


def test_hierarchy_correct_order_no_false_positive() -> None:
    text = "一、总体要求\n（一）明确责任\n1.落实分工\n（1）具体到人\n"
    assert not _issues(proofread(text), "pf.p1.hierarchy_skip")


# ---------------------------------------------------------------------------
# 4.4 只读与误报防线
# ---------------------------------------------------------------------------


def test_read_only_hash_and_no_rewritten_text() -> None:
    before = hashlib.sha256(CLEAN_DOC.encode("utf-8")).hexdigest()
    res = proofread(CLEAN_DOC, fields=_full_fields())
    after = hashlib.sha256(CLEAN_DOC.encode("utf-8")).hexdigest()

    assert before == after
    assert not hasattr(res, "text")
    dumped = res.to_dict()
    assert "text" not in dumped
    assert "rewritten" not in dumped

    # 有问题的稿件同样只读：问题清单不能携带改写后的正文
    dirty = "要高度重视，请按此布署落实。。"
    before = hashlib.sha256(dirty.encode("utf-8")).hexdigest()
    proofread(dirty, fields=_full_fields())
    assert hashlib.sha256(dirty.encode("utf-8")).hexdigest() == before


def test_clean_document_has_no_p0_p1() -> None:
    res = proofread(CLEAN_DOC, fields=_full_fields())
    assert res.counts["P0"] == 0
    assert res.counts["P1"] == 0
    # 干净成稿连 P2 也不应命中——证明规则不是见到文本就开火
    assert res.issues == []
    assert res.blocking is False


def test_halfwidth_punctuation_guard() -> None:
    # 中文语境里的半角逗号要报
    assert _issues(
        proofread("2024年增长10.5%,居首位。"), "pf.p1.punct_halfwidth"
    )
    # 网址、邮箱、数值、英文路径内的半角标点不报
    assert not _issues(
        proofread("详见 https://example.gov.cn/a,b"), "pf.p1.punct_halfwidth"
    )
    assert not _issues(proofread("A/B 测试结果如下"), "pf.p1.punct_halfwidth")
    assert not _issues(
        proofread("联系邮箱：abc.def@example.gov.cn"), "pf.p1.punct_halfwidth"
    )
    assert not _issues(proofread("2024年产量10.5%。"), "pf.p1.punct_halfwidth")


def test_halfwidth_locator_survives_masking() -> None:
    # 掩蔽 URL 后命中位置仍是原文真实下标
    text = "详见 https://example.gov.cn/a,b，另见附件,第3页。"
    hits = _issues(proofread(text), "pf.p1.punct_halfwidth")
    assert len(hits) == 1
    loc = hits[0].locator
    assert text[loc.char_start : loc.char_end] == ","


def test_placeholder_does_not_flag_standard_doc_number() -> None:
    # 「国发〔2024〕5号」的年份括号是发文字号规范写法，不是占位符
    assert not _issues(
        proofread("依据国发〔2024〕5号文件执行。"), "pf.p1.placeholder"
    )
    assert _issues(proofread("依据国发〔20XX〕5号文件执行。"), "pf.p1.placeholder")
