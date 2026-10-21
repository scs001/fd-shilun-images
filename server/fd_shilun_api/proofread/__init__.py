"""公文校对（tasks 4.1-4.4）：规则透明、只报告不改写。

模块：
- rules   规则表结构与加载器（带版本号，可增删）
- engine  匹配引擎（P0 不可忽略、只读、输出问题清单）
"""

from __future__ import annotations

from .engine import Issue, ProofreadResult, default_ruleset, proofread, ruleset_version
from .rules import (
    P0NotIgnorable,
    Rule,
    RuleSet,
    RuleTableError,
    load_rules,
)

__all__ = [
    "Issue",
    "P0NotIgnorable",
    "ProofreadResult",
    "Rule",
    "RuleSet",
    "RuleTableError",
    "default_ruleset",
    "load_rules",
    "proofread",
    "ruleset_version",
]
