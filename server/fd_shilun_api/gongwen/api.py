"""公文排版路由（task 3.x 的对外出口）。

响应把 DOCX/PDF 以 base64 放在 JSON 里，而不是 multipart 或二次下载：
- 前端只依赖标准 JSON 通道，不必处理文件流与临时 URL；
- 验收报告与交付物在同一响应里，调用方不会拿到「报告说通过但文件是旧的」；
- 代价是体积膨胀约 33%，公文 DOCX 通常几十 KB，可接受。

路由不在这里挂载到 main.py：装配由集成任务统一做（避免并发改动入口文件）。
"""

from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_user
from . import deliver, gate
from .elements import GongwenDocument
from .fonts import MissingFontError

router = APIRouter(prefix="/api/v1/gongwen", tags=["gongwen"])


class RenderRequest(GongwenDocument):
    """请求体 = 公文要素（平铺）+ 是否要 PDF。"""

    want_pdf: bool = False


class DeliverRequest(RenderRequest):
    """交付请求：在渲染参数上增加「只判定不产文件」的 dry_run。

    `citations` 是本稿引用的来源清单（同 writing 的 citations 形状：id/kind/ref_id/meta）。
    给了就按三级核验处置正文里的 [[cite:id]] 标记，未给时按空集合处理——
    此时任何标记都不可解，enforce 下会被剥除（宁删勿假）。
    """

    dry_run: bool = False
    citations: list[dict] | None = None


@router.post("/render")
def post_render(
    req: RenderRequest, user_id: str = Depends(require_user)
) -> dict:
    """渲染公文：返回验收报告、可交付判定与 base64 交付物。"""
    try:
        result = deliver.render(req, want_pdf=req.want_pdf)
    except MissingFontError as exc:
        # 严格模式是部署侧开关：属于服务当前无法满足的交付条件，不是请求错误
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except deliver.PdfExportError as exc:
        # 转换器不可用是上游依赖问题（DOCX 已能正常产出）
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "report": result.report.to_dict(),
        "deliverable": result.report.deliverable,
        "docx_base64": base64.b64encode(result.docx).decode("ascii"),
        "pdf_base64": (
            base64.b64encode(result.pdf).decode("ascii") if result.pdf else None
        ),
        "fonts": result.fonts.to_dict(),
    }


@router.post("/deliver")
def post_deliver(
    req: DeliverRequest, user_id: str = Depends(require_user)
) -> dict:
    """成稿交付闸门：排版验收 + 规则校对，有 P0 即拒绝交付（task 5.1）。

    存在 P0 问题时返回 409 + 报告（含定位与建议），且**不返回任何交付物**——
    否则下游只要忽略 `deliverable` 字段就能拿到违规成稿，闸门形同虚设。
    """
    try:
        outcome = gate.evaluate(
            req,
            want_pdf=req.want_pdf,
            with_artifacts=not req.dry_run,
            citations=req.citations,
        )
    except MissingFontError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except deliver.PdfExportError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    body = {
        "report": outcome.report.to_dict(),
        "deliverable": outcome.report.deliverable,
        "dry_run": req.dry_run,
        "docx_base64": (
            base64.b64encode(outcome.docx).decode("ascii") if outcome.docx else None
        ),
        "pdf_base64": (
            base64.b64encode(outcome.pdf).decode("ascii") if outcome.pdf else None
        ),
        "fonts": outcome.fonts.to_dict() if outcome.fonts else None,
    }
    if not outcome.report.deliverable:
        raise HTTPException(status_code=409, detail=body)
    return body


__all__ = ["router", "DeliverRequest", "RenderRequest"]
