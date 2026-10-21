"""公文格式校验（GB/T 9704-2012）：发文字号与成文日期。

单一口径：排版侧的要素校验（task 3.3）与校对侧的 P0 规则（task 4.2）都调这里，
避免两处各写一套正则后互相打架——同一个文号不能「排版说合法、校对说非法」。

依据：
- 发文字号由发文机关代字、年份、发文顺序号组成；年份用六角括号〔〕括入，
  发文顺序号不加「第」字、不编虚位（即 1 不编为 01）。
- 成文日期用阿拉伯数字将年、月、日标全，年份标全称，月、日不编虚位。
校验失败时给出**规范写法**（normalized/suggestion），报错要能直接照抄。
"""

from __future__ import annotations

import re
from datetime import date as _date

from pydantic import BaseModel

RULE_DOC_NUMBER = "gov.doc_number_format"
RULE_DOC_DATE = "gov.date_format"

# 六角括号是唯一的合法括号；方头括号【】、方括号[]、圆括号() 都是常见错误
_BRACKETS_WRONG = re.compile(r"[\[\]【】()（）]")
_SIX_CORNER = re.compile(r"〔(\d{4})〕")
# 序号从 1 起、不编虚位：正则层面就拒掉 05/00，避免「合法」分支放过补零
_DOC_NO_OK = re.compile(
    r"^(?P<prefix>[^\s〔〕\[\]【】()（）]+)〔(?P<year>\d{4})〕(?P<serial>[1-9]\d*)号$"
)
# 宽松拆解：把括号与「第」都容忍下来，只为给出可照抄的规范写法
_DOC_NO_LOOSE = re.compile(
    r"^(?P<prefix>[^\s\[\]【】()（）〔〕]+)"
    r"(?P<lb>[〔\[\]【】()（）])?"
    r"(?P<year>\d{4})"
    r"(?P<rb>[〕\[\]【】()（）])?"
    r"(?P<di>第)?"
    r"(?P<serial>\d+)"
    r"号?$"
)
_CN_DATE_OK = re.compile(r"^(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日$")
_LOOSE_DATE = re.compile(
    r"^(?P<y>\d{4})\s*[年\-/.]\s*(?P<m>\d{1,2})\s*[月\-/.]\s*(?P<d>\d{1,2})\s*日?$"
)


class FormatCheck(BaseModel):
    """单项格式校验结果：可机读的结论 + 规范写法。"""

    rule_id: str
    ok: bool
    value: str
    normalized: str | None = None
    suggestion: str | None = None
    reason: str = ""

    @property
    def status(self) -> str:
        return "pass" if self.ok else "fail"


def check_doc_number(value: str) -> FormatCheck:
    """发文字号：机关代字＋〔年份〕＋不编虚位、不带「第」的序号。"""
    raw = (value or "").strip()
    if not raw:
        return FormatCheck(
            rule_id=RULE_DOC_NUMBER,
            ok=False,
            value=raw,
            reason="发文字号缺失",
            suggestion="机关代字〔年份〕序号号，如：国发〔2024〕5号",
        )
    if _DOC_NO_OK.match(raw):
        return FormatCheck(
            rule_id=RULE_DOC_NUMBER, ok=True, value=raw, normalized=raw, reason="发文字号格式合法"
        )

    m = _DOC_NO_LOOSE.match(raw)
    if not m:
        return FormatCheck(
            rule_id=RULE_DOC_NUMBER,
            ok=False,
            value=raw,
            reason="发文字号无法解析为「机关代字〔年份〕序号号」",
            suggestion="机关代字〔年份〕序号号，如：国发〔2024〕5号",
        )

    problems: list[str] = []
    prefix = m.group("prefix")
    year = m.group("year")
    serial = m.group("serial")
    if m.group("lb") not in (None, "〔") or m.group("rb") not in (None, "〕"):
        problems.append("年份应使用六角括号〔〕")
    if m.group("di"):
        problems.append("序号不加「第」字")
    if len(serial) > 1 and serial.startswith("0"):
        problems.append("序号不编虚位（1 不编为 01）")
    if _BRACKETS_WRONG.search(raw):
        if "年份应使用六角括号〔〕" not in problems:
            problems.append("年份应使用六角括号〔〕")

    normalized = f"{prefix}〔{year}〕{int(serial)}号"
    # 去掉重复问题项（括号可能被两条规则同时发现）
    problems = list(dict.fromkeys(problems))
    return FormatCheck(
        rule_id=RULE_DOC_NUMBER,
        ok=False,
        value=raw,
        normalized=normalized,
        suggestion=normalized,
        reason="；".join(problems) if problems else "发文字号格式不规范",
    )


def check_document_date(value: str) -> FormatCheck:
    """成文日期：阿拉伯数字标全年月日，月日不编虚位，且必须是真实日期。"""
    raw = (value or "").strip()
    if not raw:
        return FormatCheck(
            rule_id=RULE_DOC_DATE,
            ok=False,
            value=raw,
            reason="成文日期缺失",
            suggestion="2024年5月1日",
        )

    m = _CN_DATE_OK.match(raw)
    if m:
        y, mo, d = int(m.group("y")), int(m.group("m")), int(m.group("d"))
        try:
            _date(y, mo, d)
        except ValueError:
            return FormatCheck(
                rule_id=RULE_DOC_DATE,
                ok=False,
                value=raw,
                normalized=f"{y}年{mo}月{d}日",
                suggestion=f"{y}年{mo}月{d}日",
                reason="成文日期不是有效日期",
            )
        if m.group("m").startswith("0") and len(m.group("m")) > 1:
            return FormatCheck(
                rule_id=RULE_DOC_DATE,
                ok=False,
                value=raw,
                normalized=f"{y}年{mo}月{d}日",
                suggestion=f"{y}年{mo}月{d}日",
                reason="月份不编虚位（1 不编为 01）",
            )
        if m.group("d").startswith("0") and len(m.group("d")) > 1:
            return FormatCheck(
                rule_id=RULE_DOC_DATE,
                ok=False,
                value=raw,
                normalized=f"{y}年{mo}月{d}日",
                suggestion=f"{y}年{mo}月{d}日",
                reason="日期不编虚位（1 不编为 01）",
            )
        return FormatCheck(
            rule_id=RULE_DOC_DATE, ok=True, value=raw, normalized=raw, reason="成文日期格式合法"
        )

    loose = _LOOSE_DATE.match(raw)
    if loose:
        y, mo, d = int(loose.group("y")), int(loose.group("m")), int(loose.group("d"))
        try:
            _date(y, mo, d)
        except ValueError:
            return FormatCheck(
                rule_id=RULE_DOC_DATE,
                ok=False,
                value=raw,
                reason="成文日期不是有效日期",
                suggestion=f"{y}年{mo}月{d}日",
            )
        normalized = f"{y}年{mo}月{d}日"
        return FormatCheck(
            rule_id=RULE_DOC_DATE,
            ok=False,
            value=raw,
            normalized=normalized,
            suggestion=normalized,
            reason="成文日期应写作「YYYY年M月D日」，月、日不编虚位",
        )

    return FormatCheck(
        rule_id=RULE_DOC_DATE,
        ok=False,
        value=raw,
        reason="成文日期无法解析为「YYYY年M月D日」",
        suggestion="2024年5月1日",
    )
