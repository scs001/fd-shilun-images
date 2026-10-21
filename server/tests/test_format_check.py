"""公文格式校验单测（task 3.3 / 4.2 共用口径）。

任务的验收样例直接写成用例：
- 「国发〔2024〕5号」通过
- 「国发[2024]第05号」「2024年05月01日」被拒并给出正确写法
"""

from __future__ import annotations

import pytest

from fd_shilun_api.format_check import check_doc_number, check_document_date


@pytest.mark.parametrize(
    "value",
    [
        "国发〔2024〕5号",
        "国办发〔2023〕12号",
        "教育部令〔2020〕3号",
        "国发〔2024〕100号",
    ],
)
def test_doc_number_accepts_six_corner_no_padding(value: str) -> None:
    res = check_doc_number(value)
    assert res.ok, res.reason
    assert res.normalized == value
    assert res.status == "pass"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("国发[2024]第05号", "国发〔2024〕5号"),
        ("国发【2024】5号", "国发〔2024〕5号"),
        ("国发（2024）5号", "国发〔2024〕5号"),
        ("国发〔2024〕第5号", "国发〔2024〕5号"),
        ("国发〔2024〕05号", "国发〔2024〕5号"),
    ],
)
def test_doc_number_rejects_and_suggests(value: str, expected: str) -> None:
    res = check_doc_number(value)
    assert not res.ok
    assert res.status == "fail"
    assert res.suggestion == expected
    assert res.normalized == expected
    assert res.reason


def test_doc_number_missing_and_unparsable() -> None:
    assert not check_doc_number("").ok
    res = check_doc_number("一份通知")
    assert not res.ok
    assert res.suggestion == "机关代字〔年份〕序号号，如：国发〔2024〕5号"


@pytest.mark.parametrize("value", ["2024年5月1日", "2019年12月31日", "2024年10月8日"])
def test_date_accepts_no_padding(value: str) -> None:
    res = check_document_date(value)
    assert res.ok, res.reason
    assert res.normalized == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024年05月01日", "2024年5月1日"),
        ("2024-05-01", "2024年5月1日"),
        ("2024/5/1", "2024年5月1日"),
        ("2024年05月1日", "2024年5月1日"),
    ],
)
def test_date_rejects_padding_and_suggests(value: str, expected: str) -> None:
    res = check_document_date(value)
    assert not res.ok
    assert res.suggestion == expected
    assert res.normalized == expected


def test_date_rejects_impossible_day() -> None:
    res = check_document_date("2024年2月30日")
    assert not res.ok
    assert "有效日期" in res.reason
    assert res.suggestion == "2024年2月30日"  # 只给写法，不臆造另一个日期


def test_date_missing() -> None:
    res = check_document_date("")
    assert not res.ok
    assert res.suggestion == "2024年5月1日"
