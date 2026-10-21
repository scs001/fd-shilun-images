"""引用核验结果模型（task 1.1）：三态判定 + 逐条依据。

判定口径（design 决策 2）：只有「确定性反证」才 block；「查不到/核不了」一律
pending_review，保留在稿中并注明原因，且不计入已通过。三态不可合并——把
pending 当 pass 会让闸门失去意义，当 block 会大面积误删中文文献。

每条结果都带 rule_id（哪条规则判的）、locator（在哪）、evidence（凭什么）、
reason（人话解释），使核验可复现、可审计。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..locator import Locator

Status = Literal["pass", "block", "pending_review"]
Level = Literal["resolvable", "authentic", "support"]
EvidenceSource = Literal[
    "collection",
    "crossref",
    "openalex",
    "datacite",
    "pubmed",
    "fulltext",
    "rule",
    "none",
]

REPORT_VERSION = "cite-verify-v1"

# 规则标识：每条判定对一条规则，便于统计与逐条开关
RULE_RESOLVABLE = "cite.resolvable"
RULE_METADATA = "cite.metadata"
RULE_RETRACTED = "cite.retracted"
RULE_EXISTS = "cite.exists"
RULE_SUPPORT = "cite.support"
RULE_FULLTEXT_MISSING = "cite.fulltext_missing"
RULE_UPSTREAM = "cite.upstream_unavailable"


class Evidence(BaseModel):
    """单条判定依据：来自哪个源、比对了什么、差异在哪。"""

    source: EvidenceSource
    detail: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None


class VerificationItem(BaseModel):
    """一条核验结果（一条引用 × 一级判定）。"""

    rule_id: str = Field(min_length=1, max_length=80)
    level: Level
    status: Status
    locator: Locator
    evidence: list[Evidence] = Field(default_factory=list)
    reason: str = ""

    @property
    def blocks(self) -> bool:
        return self.status == "block"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VerificationCounts(BaseModel):
    """三态计数。passed 只数 pass，不把 pending 混进来。"""

    total: int = 0
    passed: int = 0
    blocked: int = 0
    pending: int = 0

    def add(self, status: Status) -> None:
        self.total += 1
        if status == "pass":
            self.passed += 1
        elif status == "block":
            self.blocked += 1
        else:
            self.pending += 1

    @property
    def pass_rate(self) -> float | None:
        return round(self.passed / self.total, 4) if self.total else None


class VerificationReport(BaseModel):
    """一次核验的完整报告（随稿产出，可机读）。"""

    report_version: str = REPORT_VERSION
    mode: Literal["shadow", "enforce"] = "shadow"
    generated_at: str = Field(default_factory=now_iso)
    citation_count: int = 0
    counts: VerificationCounts = Field(default_factory=VerificationCounts)
    items: list[VerificationItem] = Field(default_factory=list)
    # 被拦截的引用 id（去重保序），供渲染层剥除
    blocked_citation_ids: list[str] = Field(default_factory=list)
    # 待核的引用 id（去重保序），供界面标注，不剥除
    pending_citation_ids: list[str] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return bool(self.blocked_citation_ids)


def build_report(
    items: list[VerificationItem], *, mode: Literal["shadow", "enforce"], citation_count: int
) -> VerificationReport:
    """汇总条目 → 报告（计数与 id 清单在此统一生成，避免各处口径漂移）。"""
    counts = VerificationCounts()
    blocked: list[str] = []
    pending: list[str] = []
    for item in items:
        counts.add(item.status)
        cid = item.locator.citation_id
        if not cid:
            continue
        if item.status == "block" and cid not in blocked:
            blocked.append(cid)
        elif item.status == "pending_review" and cid not in pending and cid not in blocked:
            pending.append(cid)
    return VerificationReport(
        mode=mode,
        citation_count=citation_count,
        counts=counts,
        items=items,
        blocked_citation_ids=blocked,
        pending_citation_ids=pending,
    )
