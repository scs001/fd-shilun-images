"""校对引擎（task 4.2-4.4）：只匹配与报告，绝不改写原文。

spec 约束（规则透明校对）：
- 每条问题带规则标识、位置、严重级别与修改建议；
- P0 级判定只由确定性规则产生（要素缺失、发文字号、成文日期、敏感词），
  P0 项不可被忽略（``ignored_rule_ids`` 里出现 P0 直接抛 ``P0NotIgnorable``）；
- 校对只输出问题清单，不自动改写原文——``proofread`` 在开跑前后各算一次
  SHA-256 并断言一致，把「只读」写成运行时不变式，而不是口头约定。

定位口径（复用 locator 契约）：
- 正文命中：``char_range`` + ``char_start/char_end``（左闭右开）+ 段落序号；
- 字段级规则：``field`` 定位（发文字号/成文日期）；
- 要素缺失：``document`` 定位（问题在整篇，不在某一处字符）。

半角标点规则先遮蔽 ASCII 语串（网址、邮箱、10.5%、A/B）再扫描，且遮蔽只
影响「是否命中」，不改偏移——定位始终指向原文真实下标。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..format_check import check_doc_number, check_document_date
from ..locator import Locator, field_locator
from .rules import P0NotIgnorable, Rule, RuleSet, Severity, load_rules

EVIDENCE_MAX = 80
_SEVERITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2}

# 中文语境下的半角标点集合：检出后建议换成对应全角形式。
_HALF_WIDTH_PUNCT = ",;:?!()"
# 连续标点只认中文常用标点的纯叠用；省略号「……」、破折号「——」是规范形式，不在此列。
_RE_PUNCT_REPEAT = re.compile(r"([，。！？；：、])\1+")

# 掩蔽 ASCII 语串：这些片段里的半角标点是合法用法，不能报成「中文行文用了半角」。
_MASK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://[^\s，。；！？、）】》]+", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    re.compile(r"\d+(?:[.,]\d+)+%?"),  # 10.5% / 1,000
    re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*(?:/[A-Za-z0-9._\-]+)+"),  # A/B
)

# 层次序数：depth 1「一、」→ 2「（一）」→ 3「1.」→ 4「（1）」
_HIERARCHY_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (1, re.compile(r"^\s*(?P<m>[一二三四五六七八九十百]+)、")),
    (2, re.compile(r"^\s*(?P<m>（[一二三四五六七八九十百]+）)")),
    (3, re.compile(r"^\s*(?P<m>\d{1,3}[.．])(?!\d)")),
    (4, re.compile(r"^\s*(?P<m>（\d{1,3}）)")),
)
# 「1、」是序号书写错误（标准写法为「1.」），单独检出。
_BAD_HIERARCHY = re.compile(r"^\s*(?P<m>\d{1,3}、)")
# 附件清单：「附件：1.……」「附：1.……」后续的 1./2./3. 是并列条目，不是层次序数。
_ATTACHMENT_LIST = re.compile(r"^\s*附(?:件)?\s*\d*\s*[:：]")
# 全文是否真的启用了一级序数「一、」；没有则 1./2./（一）只是并列清单，不做跳级判定。
_HAS_LEVEL_ONE = re.compile(r"^\s*[一二三四五六七八九十百]+、", re.M)
_PARENT_EXAMPLE: dict[int, str] = {2: "一、", 3: "（一）", 4: "1."}

_CHECKERS = {
    "doc_number": check_doc_number,
    "date": check_document_date,
}


class Issue(BaseModel):
    """一条校对问题；evidence 为命中的原文片段（≤80 字，便于前端展示）。"""

    rule_id: str
    severity: Severity
    category: str
    locator: Locator
    message: str
    suggestion: str
    evidence: str = Field(max_length=EVIDENCE_MAX)
    basis: str


class ProofreadResult(BaseModel):
    """校对结果：只有问题清单与计数，没有改写后的正文。"""

    version: str
    issues: list[Issue] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    ignored: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _recount(self) -> "ProofreadResult":
        # 计数永远由 issues 反推，避免调用方传错 counts 造成「清单与统计打架」。
        counts = {s: 0 for s in _SEVERITY_RANK}
        for issue in self.issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        counts["total"] = len(self.issues)
        self.counts = counts
        return self

    @property
    def blocking(self) -> bool:
        """任一 P0 即阻断（P0 为确定性判定，不可忽略）。"""
        return self.counts.get("P0", 0) > 0

    @property
    def p0_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "P0"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "issues": [i.model_dump(mode="json") for i in self.issues],
            "counts": dict(self.counts),
            "blocking": self.blocking,
            "p0_issues": [i.model_dump(mode="json") for i in self.p0_issues],
            "ignored": list(self.ignored),
        }


@lru_cache(maxsize=1)
def default_ruleset() -> RuleSet:
    """包内默认规则表（相对 ``__file__`` 解析，不依赖启动目录）。"""
    return load_rules()


def ruleset_version(result: ProofreadResult) -> str:
    """结果所用规则表版本（结论可复现的锚点）。"""
    return result.version


def proofread(
    text: str,
    *,
    fields: dict[str, str | None] | None = None,
    ignored_rule_ids: Sequence[str] = (),
    ruleset: RuleSet | None = None,
) -> ProofreadResult:
    """校对正文，返回逐条问题清单。

    参数：
    - ``text``：正文（只读；函数不会返回或保存任何改写文本）；
    - ``fields``：公文要素（title/addressee/issuer_signature/date/doc_number…）。
      传 ``None`` 表示「只校对正文、不掌握要素清单」，此时 required_field 与
      field_format 规则不执行；传 ``{}`` 表示「要素清单为空」，会如实报缺失；
    - ``ignored_rule_ids``：按场景忽略 P1/P2 规则并留痕；含 P0 直接抛
      ``P0NotIgnorable``（spec：P0 项不可被忽略）；
    - ``ruleset``：自定义规则表，默认取包内 rules_v1.json。
    """
    if text is None:  # 防调用方传 None；按空正文处理，保持函数总可调用
        text = ""
    rs = ruleset or default_ruleset()

    by_id = rs.by_id
    requested_ignored = list(dict.fromkeys(str(i).strip() for i in ignored_rule_ids if str(i).strip()))
    for rid in requested_ignored:
        rule = by_id.get(rid)
        if rule is not None and rule.severity == "P0":
            # P0 是确定性判定，忽略它等于关掉闸门——spec 明确禁止。
            raise P0NotIgnorable(f"P0 规则不可忽略：{rid}", rule_id=rid)
    ignored = [rid for rid in requested_ignored if rid in by_id]
    ignored_set = set(ignored)

    digest_before = hashlib.sha256(text.encode("utf-8")).hexdigest()

    issues: list[Issue] = []
    for rule in rs.rules:
        if rule.id in ignored_set:
            continue
        issues.extend(_run_rule(rule, text=text, fields=fields))

    issues = _dedupe(issues)
    issues.sort(
        key=lambda i: (
            _SEVERITY_RANK.get(i.severity, 9),
            i.locator.char_start is None,
            i.locator.char_start or 0,
            i.rule_id,
        )
    )

    digest_after = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # 只读不变式：任何规则都不得触碰正文；断言失败说明实现越界，测试会立刻暴露。
    assert digest_before == digest_after, "校对只读：执行前后正文哈希必须一致"

    return ProofreadResult(version=rs.version, issues=issues, ignored=ignored)


# ---------------------------------------------------------------------------
# 规则分派
# ---------------------------------------------------------------------------


def _run_rule(
    rule: Rule,
    *,
    text: str,
    fields: dict[str, str | None] | None,
) -> list[Issue]:
    kind = rule.match.get("kind")
    if kind in {"required_field", "field_format"} and fields is None:
        # 未提供要素清单时无法判定「缺没缺」，不做莫须有的 P0 判定。
        return []
    if kind == "literal":
        return _match_literal(rule, text)
    if kind == "regex":
        return _match_regex(rule, text)
    if kind == "punctuation_halfwidth":
        return _match_halfwidth(rule, text)
    if kind == "punctuation_repeat":
        return _match_repeat(rule, text)
    if kind == "hierarchy_skip":
        return _match_hierarchy(rule, text)
    if kind == "required_field":
        return _match_required(rule, fields or {})
    if kind == "field_format":
        return _match_field_format(rule, fields or {})
    return []  # 加载器已拒绝未知 kind；此处只是穷尽分支


def _issue(
    rule: Rule,
    *,
    locator: Locator,
    evidence: str,
    message: str | None = None,
    suggestion: str | None = None,
) -> Issue:
    return Issue(
        rule_id=rule.id,
        severity=rule.severity,
        category=rule.category,
        locator=locator,
        message=message or rule.message,
        suggestion=suggestion or rule.suggestion or "",
        evidence=_clip(evidence),
        basis=rule.basis,
    )


def _clip(value: str) -> str:
    return value[:EVIDENCE_MAX]


def _char_issue(rule: Rule, text: str, start: int, end: int, **kwargs: Any) -> Issue:
    """正文命中：定位到 [start, end)，段落序号按命中前的换行数计算。"""
    return _issue(
        rule,
        locator=Locator(
            kind="char_range",
            char_start=start,
            char_end=end,
            paragraph_index=text.count("\n", 0, start),
        ),
        evidence=text[start:end],
        **kwargs,
    )


def _match_literal(rule: Rule, text: str) -> list[Issue]:
    issues: list[Issue] = []
    # 错别字表用 suggestions 做「命中词→规范词」映射，其余 literal 规则无此表。
    suggestions: dict[str, str] = dict(rule.match.get("suggestions") or {})
    for pattern in rule.match.get("patterns") or ():
        replacement = suggestions.get(pattern)
        extra = (
            {"suggestion": f"「{pattern}」应写作「{replacement}」"} if replacement else {}
        )
        start = text.find(pattern)
        while start != -1:
            issues.append(_char_issue(rule, text, start, start + len(pattern), **extra))
            start = text.find(pattern, start + len(pattern))
    return issues


def _match_regex(rule: Rule, text: str) -> list[Issue]:
    issues: list[Issue] = []
    for pattern in rule.match.get("patterns") or ():
        for match in re.finditer(pattern, text):
            if match.end() <= match.start():  # 零宽匹配没有可定位的原文
                continue
            issues.append(_char_issue(rule, text, match.start(), match.end()))
    return issues


def _mask_ascii_spans(text: str) -> bytearray:
    """标记网址/邮箱/数值/路径片段；只用于跳过判定，不改动任何偏移。"""
    masked = bytearray(len(text))
    for pattern in _MASK_PATTERNS:
        for match in pattern.finditer(text):
            masked[match.start() : match.end()] = b"\x01" * (match.end() - match.start())
    return masked


def _neighbor(text: str, index: int, step: int) -> str:
    """取相邻的非空格字符；换行视为边界（不跨行判断语境）。"""
    j = index + step
    while 0 <= j < len(text) and text[j] == " ":
        j += step
    if not 0 <= j < len(text):
        return ""
    if text[j] in "\r\n":
        return ""
    return text[j]


def _is_cjk(ch: str) -> bool:
    return bool(ch) and ("\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf")


def _is_ascii_alnum(ch: str) -> bool:
    return bool(ch) and ch.isascii() and ch.isalnum()


def _match_halfwidth(rule: Rule, text: str) -> list[Issue]:
    masked = _mask_ascii_spans(text)
    replacements: dict[str, str] = dict(rule.match.get("replacements") or {})
    issues: list[Issue] = []
    for index, ch in enumerate(text):
        if ch not in _HALF_WIDTH_PUNCT or masked[index]:
            continue
        prev = _neighbor(text, index, -1)
        nxt = _neighbor(text, index, +1)
        # 中文语境判定：左侧是汉字，或右侧是汉字且左侧不是 ASCII 字母数字。
        # 后者放过「A,B」这类纯 ASCII 列举，接住「10.5%,居首位」。
        if not (_is_cjk(prev) or (_is_cjk(nxt) and not _is_ascii_alnum(prev))):
            continue
        full = replacements.get(ch)
        suggestion = f"「{ch}」应改为全角「{full}」" if full else None
        issues.append(_char_issue(rule, text, index, index + 1, suggestion=suggestion))
    return issues


def _match_repeat(rule: Rule, text: str) -> list[Issue]:
    return [
        _char_issue(rule, text, m.start(), m.end())
        for m in _RE_PUNCT_REPEAT.finditer(text)
        if m.end() > m.start()
    ]


def _match_hierarchy(rule: Rule, text: str) -> list[Issue]:
    """层次序数跳级：只有「本级出现时上一级从未出现」才报，避免级联误报。

    两条保守边界（针对真实语料）：
    - 全文没有「一、」「（一）」时，1./2. 是并列清单而非层次，不判跳级；
    - 「附件：1.…… 2.……」是附件清单，不按层次序数处理。
    """
    issues: list[Issue] = []
    opened: set[int] = set()
    offset = 0
    check_jump = bool(_HAS_LEVEL_ONE.search(text))
    in_attachment_list = False

    def emit(line_no: int, line_start: int, marker: str, marker_start: int, **kwargs: Any) -> None:
        issues.append(
            _issue(
                rule,
                locator=Locator(
                    kind="char_range",
                    char_start=line_start + marker_start,
                    char_end=line_start + marker_start + len(marker),
                    paragraph_index=line_no,
                ),
                evidence=marker,
                **kwargs,
            )
        )

    for line_no, line in enumerate(text.splitlines(keepends=True)):
        # 「附件：」启用的并列条目标记，遇到非数字条目行即退出该模式。
        numeric_item = bool(_BAD_HIERARCHY.match(line) or re.match(r"^\s*\d{1,3}[.．、]", line))
        if _ATTACHMENT_LIST.match(line):
            in_attachment_list = True
        elif in_attachment_list and not numeric_item:
            in_attachment_list = False

        bad = _BAD_HIERARCHY.match(line)
        if bad:
            marker = bad.group("m")
            emit(
                line_no,
                offset,
                marker,
                bad.start("m"),
                suggestion=f"「{marker}」应写作「{marker[:-1]}.」",
            )
            # 已检出的错误不再级联到后续层级
            opened = {d for d in opened if d < 3} | {1, 2, 3}
            offset += len(line)
            continue

        depth = 0
        marker = ""
        marker_start = 0
        for candidate_depth, pattern in _HIERARCHY_PATTERNS:
            match = pattern.match(line)
            if match:
                depth = candidate_depth
                marker = match.group("m")
                marker_start = match.start("m")
                break

        if depth:
            if depth == 1:
                opened = {1}
            elif in_attachment_list:
                # 附件清单里的 1./2. 不参与层次判定，也不改变已开启的层级。
                pass
            elif not check_jump:
                # 全文没有一级序数时，1./2./（一）只是并列清单，不做跳级判定。
                opened.update(range(1, depth + 1))
            else:
                if depth - 1 not in opened:
                    parent = _PARENT_EXAMPLE.get(depth, "")
                    emit(
                        line_no,
                        offset,
                        marker,
                        marker_start,
                        suggestion=f"「{marker}」前应先出现「{parent}」；{rule.suggestion}",
                    )
                # 记录本级及其祖先：同一 section 内后续同级条目不重复报，
                # 深层条目也不会因父级缺失而连锁报错。
                opened = {d for d in opened if d < depth}
                opened.update(range(1, depth + 1))
        offset += len(line)
    return issues


def _match_required(rule: Rule, fields: dict[str, str | None]) -> list[Issue]:
    labels: dict[str, str] = dict(rule.match.get("labels") or {})
    issues: list[Issue] = []
    for field in rule.match.get("fields") or ():
        value = fields.get(field)
        if value is not None and str(value).strip():
            continue
        label = labels.get(field, field)
        issues.append(
            _issue(
                rule,
                # 要素级定位必须机读（field），否则调用方只能从证据文本里猜是哪个要素
                locator=field_locator(field),
                evidence=label,
                message=f"{rule.message}：{label}",
                suggestion=f"补全{label}",
            )
        )
    return issues


def _match_field_format(rule: Rule, fields: dict[str, str | None]) -> list[Issue]:
    field = str(rule.match.get("field") or "")
    checker_name = str(rule.match.get("checker") or "")
    checker = _CHECKERS.get(checker_name)
    if checker is None:  # 加载器已保证；保留防御分支
        return []

    if field not in fields:
        # 调用方没提供该要素（不是提供了空值），不能凭空判缺失；
        # 需要判缺失时由 required_field 按明确的要素清单负责。
        return []

    raw = fields.get(field)
    if raw is None or not str(raw).strip():
        # None / 空串都视为「没有可校验的具体值」：格式规则只校验提供了的值。
        # 缺失由 required_field 单独判定，否则同一问题会报成两条 P0（虚增阻断）。
        return []
    value = str(raw)
    result = checker(value)
    if result.ok:
        return []
    reason = f"：{result.reason}" if result.reason else ""
    return [
        _issue(
            rule,
            locator=field_locator(field),
            evidence=value,
            message=f"{rule.message}{reason}",
            suggestion=result.suggestion or rule.suggestion,
        )
    ]


def _dedupe(issues: list[Issue]) -> list[Issue]:
    """同一规则下的重复/被包含命中只保留一条。

    占位符这类规则有多条正则（〔…〕、待填、XX），一条「〔待填〕」会被两条同时
    命中；同一位置报两次等于虚增问题数。同规则内「被更宽命中完全包住」视为
    同一个问题，保留更宽的那条（它的证据更完整）。
    """
    kept: list[Issue] = []
    seen: set[tuple[str, int | None, int | None, str]] = set()
    by_rule: dict[str, list[tuple[int, int]]] = {}
    for issue in issues:
        key = (
            issue.rule_id,
            issue.locator.char_start,
            issue.locator.char_end,
            issue.evidence,
        )
        if key in seen:
            continue
        start, end = issue.locator.char_start, issue.locator.char_end
        if start is not None and end is not None:
            spans = by_rule.setdefault(issue.rule_id, [])
            if any(s <= start and end <= e for s, e in spans):
                continue
            spans.append((start, end))
        seen.add(key)
        kept.append(issue)
    return kept


__all__ = [
    "EVIDENCE_MAX",
    "Issue",
    "P0NotIgnorable",
    "ProofreadResult",
    "default_ruleset",
    "proofread",
    "ruleset_version",
]
