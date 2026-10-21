"""论文检索消费面（task 2.1/2.2）：OpenAlex 通道，经 fd-open-data-protocol。

行为契约（spec shilun-serious-writing「论文检索」）：
- POST /api/v1/search/works，body 为 {"query": ...}（标题/关键词检索，
  OpenAlex search 相关度排序）或 {"doi": "10.xxxx/..."}（精确取作品）。
- 返回结构化结果列表（标识/标题/发表年/来源/被引数），每条带 provenance
  （source="openalex"，stable_id=OpenAlex work id，license=CC0）。
- 空结果必带 reason，不得静默为空：
    quota        配额/限流（OpenAlexQuotaExceeded）
    upstream     上游异常（OpenAlexUnavailable，含 5xx）
    unreachable  网络不可达（逃逸出 provider 的 httpx 超时/连接错误等）
    not_indexed  正常无命中（含 DOI 未收录——未收录 ≠ 作品不存在）
- 分页 page/per_page（默认 1/10，per_page 上限 25）原生透传给 OpenAlex。

铁律：不自研 HTTP 抓 OpenAlex——一律走 fd_open_data_protocol 的
OpenAlexProvider（复用其 UA/超时/失败语义），本模块只做薄封装与口径映射。

挂载（main.py 由协调代理统一接线，勿在子任务里改）：
    from .search import router as search_router
    app.include_router(search_router)
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from .auth import require_user

try:  # 协议包缺失时让 ImportError 在启动期显式暴露，而不是静默降级
    from fd_open_data_protocol.openalex_provider import (
        OpenAlexProvider,
        OpenAlexQuotaExceeded,
        OpenAlexUnavailable,
    )
except ImportError as _exc:  # pragma: no cover - 环境问题，提示安装路径
    raise ImportError(
        "需要 fd-open-data-protocol 提供 OpenAlex 通道："
        "pip install -e /Users/chengsishi/finddata/fd-open-data-protocol"
    ) from _exc

router = APIRouter(prefix="/api/v1/search", tags=["paper-search"])

Reason = Literal["quota", "upstream", "unreachable", "not_indexed"]

# provider 把自己的 urllib 网络错误归一成了 OpenAlexUnavailable（→upstream）；
# 这里只兜「逃逸出 provider」的网络类异常（httpx 超时/连接错误等）→unreachable。
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, OSError)
try:
    import httpx as _httpx

    _NETWORK_ERRORS = (_httpx.HTTPError, TimeoutError, OSError)
except ImportError:  # pragma: no cover - 运行环境无 httpx 时仍有基础兜底
    pass


# ----------------------------------------------------------------- provider

def _work_id(value: object) -> str:
    """https://openalex.org/W123 → W123（允许传完整 URL，统一取末段）。"""
    return str(value or "").rstrip("/").split("/")[-1]


def _venue(work: dict) -> str | None:
    """primary_location.source.display_name → 来源（期刊/载体）。"""
    loc = work.get("primary_location")
    if not isinstance(loc, dict):
        return None
    src = loc.get("source")
    if not isinstance(src, dict):
        return None
    return src.get("display_name")


class _WorksProvider(OpenAlexProvider):
    """在协议 provider 上补一个带分页与 venue 的 works 检索入口。

    OpenAlexProvider.search_works 只接受 limit（无 page，也不透出来源字段）；
    这里复用其 _get HTTP 层（同一 UA/超时/429 与 5xx 的异常语义——不自研
    HTTP），select 多取 doi 与 primary_location，并原生透传 page/per-page。
    """

    def search_works_paged(self, query: str, *, page: int, per_page: int) -> dict:
        payload, quota = self._get(
            "works",
            {
                "search": query,
                "page": str(page),
                "per-page": str(per_page),
                "select": "id,doi,title,publication_year,cited_by_count,primary_location",
            },
        )
        rows = []
        for w in payload.get("results") or []:
            rows.append(
                {
                    "work_id": _work_id(w.get("id")),
                    "doi": w.get("doi"),
                    "title": w.get("title"),
                    "year": w.get("publication_year"),
                    "venue": _venue(w),
                    "cited_by_count": w.get("cited_by_count"),
                }
            )
        return {
            "total": (payload.get("meta") or {}).get("count"),
            "results": rows,
            "quota": quota,
        }


def _new_provider() -> OpenAlexProvider:
    return _WorksProvider()


# ------------------------------------------------------------------ models

class WorksRequest(BaseModel):
    """检索请求：query 与 doi 二选一。"""

    query: str | None = Field(default=None, max_length=512)
    doi: str | None = Field(default=None, max_length=512)
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=10, ge=1, le=25)

    @model_validator(mode="after")
    def _exactly_one_key(self) -> "WorksRequest":
        has_query = bool((self.query or "").strip())
        has_doi = bool((self.doi or "").strip())
        if has_query == has_doi:  # 都有或都无
            raise ValueError("query 与 doi 必须二选一（只提供其中一个）")
        return self


class Provenance(BaseModel):
    source: str = "openalex"
    stable_id: str
    license: str = "CC0"


class WorkItem(BaseModel):
    work_id: str  # 标识（OpenAlex work id）
    doi: str | None = None
    title: str | None = None
    year: int | None = None  # 发表年
    venue: str | None = None  # 来源（期刊/载体）
    cited_by_count: int | None = None  # 被引数
    provenance: Provenance


class WorksResponse(BaseModel):
    results: list[WorkItem] = []
    total: int | None = None
    page: int = 1
    per_page: int = 10
    reason: Reason | None = None  # 空结果必带
    detail: str | None = None


# ------------------------------------------------------------------- core

def _normalize_doi(raw: str) -> str:
    doi = raw.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi.strip().lower()


def _empty(
    *, reason: Reason, detail: str, page: int, per_page: int, total: int | None = None
) -> WorksResponse:
    return WorksResponse(
        results=[], total=total, page=page, per_page=per_page, reason=reason, detail=detail
    )


def _item(row: dict) -> WorkItem:
    work_id = str(row.get("work_id") or "")
    return WorkItem(
        work_id=work_id,
        doi=row.get("doi"),
        title=row.get("title"),
        year=row.get("year"),
        venue=row.get("venue"),
        cited_by_count=row.get("cited_by_count"),
        provenance=Provenance(stable_id=work_id),
    )


def _doi_lookup(provider: OpenAlexProvider, doi: str) -> WorksResponse:
    """DOI 精确取作品（provider.get_work）。"""
    try:
        w = provider.get_work({"doi": doi})
    except OpenAlexQuotaExceeded as exc:
        return _empty(reason="quota", detail=f"OpenAlex 配额/限流: {exc}", page=1, per_page=1)
    except OpenAlexUnavailable as exc:
        return _empty(reason="upstream", detail=f"OpenAlex 上游异常: {exc}", page=1, per_page=1)
    except _NETWORK_ERRORS as exc:
        return _empty(
            reason="unreachable",
            detail=f"网络不可达: {type(exc).__name__}: {exc}",
            page=1,
            per_page=1,
        )
    except ValueError as exc:
        # get_work 未命中抛 ValueError：未收录，不等于该作品不存在
        return _empty(
            reason="not_indexed",
            detail=f"DOI 未收录于 OpenAlex（不等于作品不存在）: {exc}",
            page=1,
            per_page=1,
        )
    except Exception as exc:  # noqa: BLE001 —— 未知异常也显式归因，不静默
        return _empty(
            reason="upstream",
            detail=f"未知异常: {type(exc).__name__}: {exc}",
            page=1,
            per_page=1,
        )
    return WorksResponse(results=[_item(w)], total=1, page=1, per_page=1)


def _query_lookup(
    provider: OpenAlexProvider, query: str, page: int, per_page: int
) -> WorksResponse:
    """标题/关键词检索（OpenAlex search，相关度排序）。"""
    try:
        data = provider.search_works_paged(query, page=page, per_page=per_page)
    except OpenAlexQuotaExceeded as exc:
        return _empty(reason="quota", detail=f"OpenAlex 配额/限流: {exc}", page=page, per_page=per_page)
    except OpenAlexUnavailable as exc:
        return _empty(reason="upstream", detail=f"OpenAlex 上游异常: {exc}", page=page, per_page=per_page)
    except _NETWORK_ERRORS as exc:
        return _empty(
            reason="unreachable",
            detail=f"网络不可达: {type(exc).__name__}: {exc}",
            page=page,
            per_page=per_page,
        )
    except Exception as exc:  # noqa: BLE001 —— 未知异常也显式归因，不静默
        return _empty(
            reason="upstream",
            detail=f"未知异常: {type(exc).__name__}: {exc}",
            page=page,
            per_page=per_page,
        )
    total = data.get("total")
    items = [_item(r) for r in data.get("results") or []]
    if not items:
        if total:
            detail = f"请求页超出命中范围（total={total}）: query={query!r}"
        else:
            detail = f"OpenAlex 无命中: query={query!r}"
        return _empty(reason="not_indexed", detail=detail, page=page, per_page=per_page, total=total)
    return WorksResponse(results=items, total=total, page=page, per_page=per_page)


def search_works(
    *,
    query: str | None = None,
    doi: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> WorksResponse:
    """检索核心（无鉴权层，路由与真实验收脚本共用入口）。"""
    page = max(int(page), 1)
    per_page = min(max(int(per_page), 1), 25)
    provider = _new_provider()
    if (doi or "").strip():
        return _doi_lookup(provider, _normalize_doi(doi))
    return _query_lookup(provider, (query or "").strip(), page, per_page)


# ------------------------------------------------------------------- route

@router.post("/works", response_model=WorksResponse)
def works(body: WorksRequest, user_id: str = Depends(require_user)) -> WorksResponse:
    """论文检索：query（标题/关键词）或 doi（精确取）。空结果必带 reason。"""
    return search_works(query=body.query, doi=body.doi, page=body.page, per_page=body.per_page)
