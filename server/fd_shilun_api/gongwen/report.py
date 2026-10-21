"""公文验收报告（task 3.5 / design 决策 7）：可机读、逐条带规则与证据。

一份公文能否交付不能靠人工目检。报告把每个结论落成三元组
（rule_id, status, locator）+ evidence：前端可按 locator 定位高亮，
流水线可按 rule_id 统计与开关，审计可复现「凭什么判通过/失败」。

与引用核验报告（verify/models.py）的口径差异：
- 核验是三态（pass/block/pending_review），因为「查不到」不能当「假」；
- 公文验收是二态 + 告警（pass/fail/warn），失真是可以确定性判定的
  （缺要素、版式偏差、格式不合规都有唯一正确答案），所以失败必须能阻断交付。
`deliverable` 只在**存在 P0 失败**时为 False：P1/P2 是建议与条件性告警，
不该让一份合规公文因为「未标注份号」而不可交付。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..locator import Locator

SCHEMA_VERSION = "gongwen-report-v1"

Status = Literal["pass", "fail", "warn"]
Severity = Literal["P0", "P1", "P2"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CheckItem(BaseModel):
    """一条验收结论：哪条规则、什么结果、在哪、凭什么。"""

    rule_id: str = Field(min_length=1, max_length=80)
    status: Status
    # 通过项也带级别：级别描述「若失败会造成多大影响」，供前端排序
    severity: Severity | None = None
    locator: Locator
    # min_length=1 只挡空串，"   " 仍能通过，故再按 strip 后判空：空证据等于没有报告
    evidence: str = Field(min_length=1)
    suggestion: str | None = None

    @field_validator("rule_id", "evidence")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("rule_id 与 evidence 不能为空白")
        return text


class AcceptanceReport(BaseModel):
    """一次渲染的验收报告（随稿产出，可机读）。"""

    schema_version: str = SCHEMA_VERSION
    doc_kind: str = "gongwen"
    generated_at: str = Field(default_factory=now_iso)
    items: list[CheckItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _items_have_evidence(self) -> "AcceptanceReport":
        # 双保险：即使调用方用 model_construct 绕过字段校验，也不放行空证据
        for index, item in enumerate(self.items):
            if not item.evidence.strip():
                raise ValueError(f"第 {index} 条验收项缺少证据（rule_id={item.rule_id!r}）")
        return self

    @property
    def counts(self) -> dict[str, int]:
        out = {"total": len(self.items), "pass": 0, "fail": 0, "warn": 0, "p0_fail": 0}
        for item in self.items:
            out[item.status] += 1
            if item.status == "fail" and item.severity == "P0":
                out["p0_fail"] += 1
        return out

    @property
    def pass_(self) -> list[CheckItem]:
        """通过项（`pass` 是关键字，故属性名加下划线）。"""
        return [item for item in self.items if item.status == "pass"]

    @property
    def failed(self) -> list[CheckItem]:
        return [item for item in self.items if item.status == "fail"]

    @property
    def warned(self) -> list[CheckItem]:
        return [item for item in self.items if item.status == "warn"]

    @property
    def deliverable(self) -> bool:
        # 只有 P0 失败阻断交付；warn 与 P1/P2 失败进报告但不改变可交付性
        return not any(
            item.status == "fail" and item.severity == "P0" for item in self.items
        )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
