"""公文校对路由（task 4.x 的对外出口）。

契约（spec「规则透明校对」）：
- 逐条给出规则标识、位置、严重级别（P0/P1/P2）与修改建议；
- P0 级判定由确定性规则产生，不可被忽略（请求里传 P0 规则 → 400）；
- 校对只读：请求体正文原样不改，响应只有问题清单，没有改写后的正文。

与渲染路由（gongwen/api.py）的分工：这里只做「只看问题、不要文件」的校对，
导出交付走 /api/v1/gongwen/render（那里排版 + 校对一起过闸）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_user
from . import P0NotIgnorable, RuleTableError, proofread
from .rules import load_rules

router = APIRouter(prefix="/api/v1/proofread", tags=["proofread"])


class ProofreadRequest(BaseModel):
    """校对请求：正文 + 可选要素字段 + 可忽略的 P1/P2 规则。"""

    text: str = Field(min_length=0)
    # 传 None 表示「只校对正文、不掌握要素清单」；传 {} 会如实报缺要素
    fields: dict[str, str | None] | None = None
    ignore_rule_ids: list[str] = Field(default_factory=list)


@router.post("")
def post_proofread(
    req: ProofreadRequest, user_id: str = Depends(require_user)
) -> dict:
    """校对正文，返回问题清单（不改写原文）。"""
    try:
        result = proofread(
            req.text,
            fields=req.fields,
            ignored_rule_ids=req.ignore_rule_ids,
        )
    except P0NotIgnorable as exc:
        # P0 是确定性判定，忽略它等于关掉闸门——按请求错误拒绝，不静默降级
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.to_dict()


@router.get("/rules")
def get_rules(user_id: str = Depends(require_user)) -> dict:
    """规则表（带版本号）：供前端展示「凭什么判」与逐条开关。"""
    try:
        ruleset = load_rules()
    except RuleTableError as exc:  # pragma: no cover - 规则表损坏属部署事故
        raise HTTPException(status_code=500, detail=f"规则表加载失败：{exc}") from exc
    return {
        "version": ruleset.version,
        "rules": [rule.model_dump(mode="json") for rule in ruleset.rules],
    }


__all__ = ["router", "ProofreadRequest"]
