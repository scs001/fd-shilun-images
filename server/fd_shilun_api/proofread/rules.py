"""校对规则表结构与加载器（task 4.1）。

规则表是结构化数据，引擎只做匹配与报告——规则可版本化、可逐条开关、可解释
（设计决策 5）。规则外置的约束由本模块承担：表必须有版本号、id 唯一、级别只有
P0/P1/P2、正则必须可编译、每条规则必须给「依据」，未知匹配方式直接拒绝。

规则表格式（rules_v1.json）::

    {
      "version": "proofread-v1",
      "rules": [
        {
          "id": "pf.p1.cliche",
          "severity": "P1",                 # P0 / P1 / P2
          "category": "套话",
          "match": { ... },                 # 见下方 match.kind
          "message": "问题描述",
          "suggestion": "修改建议",          # required_field 可省略（引擎拼）
          "basis": "规则依据（必填）"
        }
      ]
    }

支持的 ``match.kind``：

- ``literal``：``patterns`` 为精确字符串列表；
- ``regex``：``patterns`` 为 Python 正则列表（加载时逐条编译校验）；
- ``punctuation_halfwidth``：无 patterns，引擎检测中文语境里的半角 , ; : ? ! ( )；
- ``punctuation_repeat``：无 patterns，引擎检测「。。」「，，」这类叠用；
- ``hierarchy_skip``：无 patterns，引擎检查层次序数 一、→（一）→ 1.→（1）跳级；
- ``required_field``：``fields`` 为必需要素字段名列表，缺失即 P0；
- ``field_format``：``field`` + ``checker``（doc_number / date），
  由引擎调用 format_check 的同一口径校验，避免两处正则互相打架。

P0 只允许确定性可判的项（要素缺失、发文字号、成文日期、敏感词），且 P0 项
不可被忽略（见 engine.P0NotIgnorable）——实现 spec「P0 级判定 MUST 由确定性
规则产生」与「P0 不可忽略」。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Severity = Literal["P0", "P1", "P2"]

SEVERITIES: tuple[Severity, ...] = ("P0", "P1", "P2")
MATCH_KINDS: frozenset[str] = frozenset(
    {
        "literal",
        "regex",
        "punctuation_halfwidth",
        "punctuation_repeat",
        "hierarchy_skip",
        "required_field",
        "field_format",
    }
)
# field_format 的 checker 与 format_check 函数一一对应；不在表内的直接拒绝，
# 防止规则表引用不存在的校验口径。
FIELD_CHECKERS: frozenset[str] = frozenset({"doc_number", "date"})

_DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "rules_v1.json"


class RuleTableError(ValueError):
    """规则表不合法。``rule_id`` 指向出问题的规则，便于逐条定位。"""

    def __init__(self, message: str, rule_id: str | None = None) -> None:
        self.rule_id = rule_id
        super().__init__(f"[{rule_id}] {message}" if rule_id else message)


class P0NotIgnorable(RuleTableError):
    """试图忽略 P0 规则。spec：P0 判定由确定性规则产生，不可被忽略。"""


class Rule(BaseModel):
    """单条校对规则（不可变，规则表可安全共享）。"""

    model_config = ConfigDict(frozen=True)

    id: str
    severity: Severity
    category: str
    match: dict[str, Any]
    message: str
    suggestion: str | None = None
    basis: str


class RuleSet(BaseModel):
    """带版本号的规则集合；支持增补与裁剪（返回新集合，不改原对象）。"""

    model_config = ConfigDict(frozen=True)

    version: str
    rules: tuple[Rule, ...]

    @property
    def by_id(self) -> dict[str, Rule]:
        return {r.id: r for r in self.rules}

    @property
    def severities(self) -> dict[str, tuple[Rule, ...]]:
        """按级别分组，便于调用方统计/展示规则覆盖面。"""
        grouped: dict[str, list[Rule]] = {s: [] for s in SEVERITIES}
        for rule in self.rules:
            grouped[rule.severity].append(rule)
        return {s: tuple(items) for s, items in grouped.items()}

    def with_rules(self, *rules: Rule | Mapping[str, Any]) -> "RuleSet":
        """增补规则，返回新集合；重复 id 会被拒绝（不静默覆盖）。"""
        merged = list(self.rules) + [_validate_rule(_as_mapping(r)) for r in rules]
        return _build(self.version, merged)

    def without(self, ids: Iterable[str]) -> "RuleSet":
        """移除规则，返回新集合；未知 id 报错（防止拼错后误以为已关闭）。"""
        drop = _as_id_set(ids)
        known = {r.id for r in self.rules}
        unknown = sorted(drop - known)
        if unknown:
            raise RuleTableError(f"要移除的规则不存在：{unknown[0]}", rule_id=unknown[0])
        return _build(self.version, [r for r in self.rules if r.id not in drop])

    def __len__(self) -> int:  # pragma: no cover - 便于交互式检查
        return len(self.rules)


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------


def load_rules(
    path: str | Path | None = None,
    *,
    extra: Sequence[Rule | Mapping[str, Any]] | None = None,
    removed: Iterable[str] = (),
) -> RuleSet:
    """加载规则表并完成全部结构校验。

    - ``path``：默认取包内 rules_v1.json（相对 ``__file__``，不依赖 cwd）；
    - ``extra``：追加规则（dict 或 Rule），用于按需扩展；
    - ``removed``：移除规则 id，用于按场景裁剪。

    任何不合法都抛 ``RuleTableError`` 并带上规则 id——规则表是校对结论的
    依据，坏规则必须在加载期暴露，不能等到匹配时静默跳过。
    """
    version, raw_rules = _read_table(Path(path) if path is not None else _DEFAULT_RULES_PATH)
    rules = [_validate_rule(raw) for raw in raw_rules]
    if extra:
        rules.extend(_validate_rule(_as_mapping(item)) for item in extra)
    ruleset = _build(version, rules)

    drop = _as_id_set(removed)
    if drop:
        ruleset = ruleset.without(drop)
    return ruleset


def _read_table(path: Path) -> tuple[str, list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuleTableError(f"规则表不存在：{path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise RuleTableError(f"规则表不可读（{path}）：{type(exc).__name__}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuleTableError(f"规则表不是合法 JSON（{path}）：{exc}") from exc

    if not isinstance(payload, dict):
        raise RuleTableError(f"规则表顶层必须是对象（{path}）")
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        # 规则版本是校对结论可复现的前提，缺版本直接拒绝整张表。
        raise RuleTableError("规则表缺少 version")
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleTableError("规则表 rules 必须为非空数组")
    return version.strip(), raw_rules


def _as_mapping(item: Rule | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(item, Rule):
        data = item.model_dump()
        # 直接构造的 Rule 未经过加载器校验（例如正则没编译），此处重新走一遍。
        return dict(data)
    if isinstance(item, Mapping):
        return dict(item)
    raise RuleTableError(f"规则条目必须是对象，收到 {type(item).__name__}")


def _as_id_set(ids: Iterable[str]) -> set[str]:
    if isinstance(ids, str):  # 防呆：单条 id 字符串不该被逐字符拆开
        ids = (ids,)
    out = {str(i).strip() for i in ids if str(i).strip()}
    return out


def _build(version: str, rules: Sequence[Rule]) -> RuleSet:
    if not isinstance(version, str) or not version.strip():
        raise RuleTableError("规则表缺少 version")
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise RuleTableError(f"规则 id 重复：{rule.id}", rule_id=rule.id)
        seen.add(rule.id)
    return RuleSet(version=version.strip(), rules=tuple(rules))


def _validate_rule(raw: Mapping[str, Any]) -> Rule:
    rid_raw = raw.get("id")
    rid = rid_raw.strip() if isinstance(rid_raw, str) else ""
    if not rid:
        raise RuleTableError("规则缺少非空 id", rule_id="<unknown>")

    severity = raw.get("severity")
    if severity not in SEVERITIES:
        raise RuleTableError(
            f"非法严重级别 {severity!r}（只允许 P0/P1/P2）", rule_id=rid
        )

    category = raw.get("category")
    if not isinstance(category, str) or not category.strip():
        raise RuleTableError("规则缺少 category", rule_id=rid)

    basis = raw.get("basis")
    if not isinstance(basis, str) or not basis.strip():
        # 「校对要给依据」：没有依据的规则等于不可解释的判定。
        raise RuleTableError("规则缺少 basis（校对依据）", rule_id=rid)

    message = raw.get("message")
    if not isinstance(message, str) or not message.strip():
        raise RuleTableError("规则缺少 message", rule_id=rid)

    match = raw.get("match")
    if not isinstance(match, Mapping):
        raise RuleTableError("规则缺少 match 对象", rule_id=rid)
    match = dict(match)

    kind = match.get("kind")
    if kind not in MATCH_KINDS:
        raise RuleTableError(f"未知匹配方式 {kind!r}", rule_id=rid)

    if kind in {"literal", "regex"}:
        patterns = match.get("patterns")
        if not isinstance(patterns, list) or not patterns:
            raise RuleTableError(f"{kind} 规则必须提供非空 patterns", rule_id=rid)
        cleaned: list[str] = []
        for pat in patterns:
            if not isinstance(pat, str) or not pat:
                raise RuleTableError("patterns 必须是非空字符串列表", rule_id=rid)
            if kind == "regex":
                try:
                    re.compile(pat)
                except re.error as exc:
                    raise RuleTableError(
                        f"正则无法编译：{pat!r}（{exc}）", rule_id=rid
                    ) from exc
            cleaned.append(pat)
        match["patterns"] = cleaned
        suggestions = match.get("suggestions")
        if suggestions is not None:
            if not isinstance(suggestions, Mapping):
                raise RuleTableError("suggestions 必须是 文本→建议 的对象", rule_id=rid)
            match["suggestions"] = {str(k): str(v) for k, v in suggestions.items()}

    elif kind == "required_field":
        fields = match.get("fields")
        if not isinstance(fields, list) or not fields or not all(
            isinstance(f, str) and f.strip() for f in fields
        ):
            raise RuleTableError("required_field 必须提供非空 fields", rule_id=rid)
        labels = match.get("labels")
        if labels is not None and not isinstance(labels, Mapping):
            raise RuleTableError("labels 必须是 字段→中文名 的对象", rule_id=rid)
        match["fields"] = [f.strip() for f in fields]

    elif kind == "field_format":
        field = match.get("field")
        checker = match.get("checker")
        if not isinstance(field, str) or not field.strip():
            raise RuleTableError("field_format 缺少 field", rule_id=rid)
        if checker not in FIELD_CHECKERS:
            raise RuleTableError(
                f"field_format 的 checker 只支持 {sorted(FIELD_CHECKERS)}，收到 {checker!r}",
                rule_id=rid,
            )

    suggestion = raw.get("suggestion")
    if kind != "required_field":
        if not isinstance(suggestion, str) or not suggestion.strip():
            raise RuleTableError("规则缺少 suggestion（修改建议）", rule_id=rid)
    elif suggestion is not None and not isinstance(suggestion, str):
        raise RuleTableError("suggestion 必须是字符串", rule_id=rid)

    try:
        return Rule(
            id=rid,
            severity=severity,
            category=category.strip(),
            match=match,
            message=message.strip(),
            suggestion=suggestion.strip() if isinstance(suggestion, str) else None,
            basis=basis.strip(),
        )
    except Exception as exc:  # noqa: BLE001 - pydantic 错误统一归一为表错误
        raise RuleTableError(f"规则字段不合法：{exc}", rule_id=rid) from exc


__all__ = [
    "FIELD_CHECKERS",
    "MATCH_KINDS",
    "P0NotIgnorable",
    "Rule",
    "RuleSet",
    "RuleTableError",
    "SEVERITIES",
    "Severity",
    "load_rules",
]
