"""论文检索消费面（task 2.1-2.3）：OpenAlex 默认通道 + 中文授权通道。

行为契约（spec shilun-serious-writing「论文检索」）：
- POST /api/v1/search/works，body 为 {"query": ...}（标题/关键词检索，
  OpenAlex search 相关度排序）或 {"doi": "10.xxxx/..."}（精确取作品）。
- 返回结构化结果列表（标识/标题/发表年/来源/被引数），每条带 provenance
  （source="openalex"，stable_id=OpenAlex work id，license=CC0）。
- 空结果必带 reason，不得静默为空：
    quota                  配额/限流（OpenAlexQuotaExceeded）
    upstream               上游异常（OpenAlexUnavailable，含 5xx）
    unreachable            网络不可达（逃逸出 provider 的 httpx 超时/连接错误等）
    not_indexed            正常无命中（含 DOI 未收录——未收录 ≠ 作品不存在）
    no_authorized_channel  仅在关闭兜底开关（回滚档）时由中文检索产生
- 分页 page/per_page（默认 1/10，per_page 上限 25）原生透传给 OpenAlex。

中文文献：query 含汉字时先走 channels.py 的授权通道（不得用浏览器自动化抓
商业库）；无可用通道或通道无命中则走开放元数据兜底（Crossref → OpenAlex），
逐条标注 coverage=partial / metadata_only=true，响应附 coverage_note。原因码
no_authorized_channel 只保留给需要原文全文的能力，不再由关键词检索产生
（FD_SHILUN_CN_OPEN_FALLBACK 关闭时例外，用于回滚）。
DOI 查询不受影响——DOI 永远走 OpenAlex 精确取。

可选精排（请求体 rerank=true，仅影响中文兜底路径）：开放元数据是词面覆盖率
筛选，能保证「沾边」但不能保证「相关」。候选多于一条时请 LLM 复核一次，
只保留模型判定相关的条目；未配置网关、调用失败、输出不可解析一律退回全部
候选——一次模型故障不得把检索结果清空。模型只做取舍，不新增、不改写结果。

铁律：不自研 HTTP 抓 OpenAlex——一律走 fd_open_data_protocol 的
OpenAlexProvider（复用其 UA/超时/失败语义），本模块只做薄封装与口径映射。

挂载（main.py 由协调代理统一接线，勿在子任务里改）：
    from .search import router as search_router
    app.include_router(search_router)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Literal, Sequence

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_serializer, model_validator

from .auth import require_user
from . import llm
from .channels import SearchChannel, search_chinese
from .open_metadata import search_open_metadata

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

Reason = Literal[
    "quota", "upstream", "unreachable", "not_indexed", "no_authorized_channel"
]

# 开放元数据兜底开关：默认开（未配置商业通道时中文检索仍有元数据可用）。
# 设为 0/false/off 回到旧口径（中文仅经授权通道 → no_authorized_channel），用于回滚。
CN_OPEN_FALLBACK_ENV = "FD_SHILUN_CN_OPEN_FALLBACK"

# 中文文献判定：query 含汉字即视为中文文献检索（spec：中文文献仅经授权通道）。
# 覆盖 CJK 统一表意文字、扩展 A、兼容表意文字与扩展 B-F；日文汉字同属 CJK，
# 同样走授权通道，合规边界不因语种细分而放宽。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
)


def _has_cjk(text: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in _CJK_RANGES)


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
    """检索请求：query 与 doi 二选一。

    `channel_credentials` 是使用者自备的授权通道凭据（BYOK），键为通道 id。
    走 TLS 传输；服务端只在本次调用内使用，不持久化、不写日志、不回显。
    """

    query: str | None = Field(default=None, max_length=512)
    doi: str | None = Field(default=None, max_length=512)
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=10, ge=1, le=25)
    channel_credentials: dict[str, str] | None = Field(default=None, repr=False)
    # 可选精排：只在中文兜底（开放元数据）命中后生效，未配置网关时无副作用
    rerank: bool = False

    @model_validator(mode="after")
    def _exactly_one_key(self) -> "WorksRequest":
        has_query = bool((self.query or "").strip())
        has_doi = bool((self.doi or "").strip())
        if has_query == has_doi:  # 都有或都无
            raise ValueError("query 与 doi 必须二选一（只提供其中一个）")
        return self


class Authorization(BaseModel):
    """授权依据（机器可读）：哪个通道、依据什么授权。"""

    channel: str
    basis: str


class Provenance(BaseModel):
    source: str = "openalex"
    stable_id: str
    license: str = "CC0"
    # 仅中文授权通道填充；OpenAlex 路径保持 None，既有响应照旧通过校验。
    authorization: Authorization | None = None
    # 覆盖度：full=该源声称完整；partial=已知不完整（开放元数据兜底中文检索）。
    # 仅在非 full 时出现——OpenAlex 主路径不写，响应与改动前逐字节一致。
    coverage: Literal["partial"] | None = None
    # 只有元数据、没有原文：中文兜底路径恒为 True，提醒调用方别把它当已核验。
    metadata_only: bool | None = None
    # 部分覆盖的透明度：本条命中查询里的哪些词、覆盖比例多少。
    matched_terms: list[str] | None = None
    coverage_ratio: float | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_authorization(self, handler):  # noqa: ANN001
        data = handler(self)
        if not isinstance(data, dict):
            return data
        # 未授权时连空字段都不出现：OpenAlex 响应与改动前逐字节一致
        if data.get("authorization") is None:
            data.pop("authorization", None)
        for key in ("coverage", "metadata_only", "matched_terms", "coverage_ratio"):
            if data.get(key) is None:
                data.pop(key, None)
        return data



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
    # 覆盖度说明：中文兜底路径为「覆盖不完整」的整句提示；OpenAlex 主路径不写。
    coverage_note: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_coverage_note(self, handler):  # noqa: ANN001
        data = handler(self)
        if isinstance(data, dict) and data.get("coverage_note") is None:
            data.pop("coverage_note", None)
        return data


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


def _channel_item(row: dict, *, channel: str, basis: str) -> WorkItem:
    """授权通道记录 → 既有 WorkItem 形状：schema 稳定，只多一个可选授权依据。"""
    stable_id = str(row.get("stable_id") or "")
    return WorkItem(
        work_id=stable_id,
        doi=row.get("doi"),
        title=row.get("title"),
        year=row.get("year"),
        venue=row.get("venue"),
        cited_by_count=None,  # 中文通道暂不提供被引数
        provenance=Provenance(
            source=channel,
            stable_id=stable_id,
            license=basis,  # 授权依据替代 OpenAlex 的 CC0 口径
            authorization=Authorization(channel=channel, basis=basis),
        ),
    )


COVERAGE_NOTE = (
    "中文文献经开放元数据（Crossref/OpenAlex）检索，覆盖不完整："
    "仅含已注册 DOI 或已被收录的条目，未收录的中文期刊与学位论文查不到。"
    "配置授权通道可提升覆盖。"
)


def _open_fallback_enabled() -> bool:
    """开放元数据兜底开关，默认开；显式设为 0/false/off 时回到「仅授权通道」。"""
    raw = os.environ.get(CN_OPEN_FALLBACK_ENV, "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


COVERAGE_NOTE_CROSSLINGUAL = (
    COVERAGE_NOTE + "本次中文题名无命中，已自动用英文检索词补充检索。"
)


def _translated_query(query: str) -> str | None:
    """中文检索词 → 英文检索词（跨语言补齐，LLM 增强）。

    只在该主题的中文题名一条都没命中时才调用：有些主题的文献全部著录为英文
    题名，词面比对没有可比的字符，唯一办法是换一个语言的检索词重查。

    未配置网关 → None；调用失败 → None。两条都让调用方继续走原来的确定性
    路径，因此**没配 LLM 时行为与之前完全一致**。
    """
    if not llm.is_configured():
        return None
    try:
        out = llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "把中文文献检索词翻译成等价的英文文献检索词。"
                        "只输出检索词本身，一行，不要解释、不要引号、不要编号。"
                    ),
                },
                {"role": "user", "content": query},
            ],
            temperature=0,
            max_tokens=64,
        )
    except llm.LLMError:
        return None
    text = (out or "").strip().strip('"').strip()
    # 译文必须真的换了语言，否则再查一遍只是白花一次调用
    if not text or _has_cjk(text) or not any(ch.isalpha() for ch in text):
        return None
    return text[:200]


# 测试注入点：None 时由 open_metadata 自建 httpx.Client（生产路径）。
_OPEN_METADATA_CLIENT: Any = None


# 精排提示词：只要下标数组。模型只做取舍，不生成新条目——检索结果必须可溯源到
# 上游返回的那几条，模型说什么都不能凭空多出文献。
_RERANK_PROMPT = (
    "你在复核文献检索结果的相关性。给定用户检索词与候选题名列表，"
    "选出与检索词主题相关的候选。只输出一个 JSON 整数数组，"
    "元素是候选前面的下标，例如 [0,2]。"
    "不要解释、不要代码块、不要输出任何其它文字。"
)


def _parse_rerank_indexes(raw: str, count: int) -> list[int] | None:
    """模型输出 → 保留下标；任何不可信的输出一律 None（调用方保留全部候选）。

    空数组也返回 None：模型说「一条都不相关」与「模型答非所问」在文本上一样
    不可靠，而按空数组返回会让一次检索凭空变成「查不到」——空结果的结论只能
    由确定性通道给出，不能由一次模型调用决定。
    """
    match = re.search(r"\[[^\[\]]*\]", raw or "")
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    keep: list[int] = []
    for value in parsed:
        # bool 是 int 的子类：JSON 的 true/false 不能当 0/1 用
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if not 0 <= value < count:
            return None
        if value not in keep:
            keep.append(value)
    return sorted(keep)


def _rerank_keep(query: str, titles: Sequence[str]) -> list[int] | None:
    """请模型复核候选相关性，返回保留项下标；None 表示「无法判定，全部保留」。"""
    if not llm.is_configured():
        return None
    numbered = "\n".join(f"{i}. {title}" for i, title in enumerate(titles))
    try:
        out = llm.chat(
            [
                {"role": "system", "content": _RERANK_PROMPT},
                {"role": "user", "content": f"检索词：{query}\n候选题名：\n{numbered}"},
            ],
            temperature=0,
            max_tokens=128,
        )
    except llm.LLMError:
        return None
    return _parse_rerank_indexes(out or "", len(titles))


def _apply_rerank(items: list[WorkItem], query: str) -> list[WorkItem]:
    """按模型判定过滤候选：只有多于一条时才值得多花一次调用。"""
    if len(items) <= 1:
        return items
    keep = _rerank_keep(query, [str(item.title or "") for item in items])
    if keep is None:
        return items
    return [items[i] for i in keep]


def _open_metadata_items(result) -> list[WorkItem]:
    """开放元数据命中 → WorkItem，逐条标注 partial + metadata_only。"""
    items: list[WorkItem] = []
    for row in result.records:
        stable_id = str(row.get("stable_id") or "")
        items.append(
            WorkItem(
                work_id=stable_id,
                doi=row.get("doi"),
                title=row.get("title"),
                year=row.get("year"),
                venue=row.get("venue"),
                cited_by_count=None,
                provenance=Provenance(
                    source=str(result.source or "open_metadata"),
                    stable_id=stable_id,
                    license="metadata-only",  # 只有元数据，不声称可读全文
                    coverage="partial",
                    metadata_only=True,
                    matched_terms=row.get("matched_terms") or None,
                    # 0.0 只出现在「无汉字记录、靠上游分数锚定」的情况，
                    # 那不是「覆盖 0%」而是「无法用中文词比对」，不填更诚实
                    coverage_ratio=row.get("coverage_ratio") or None,
                ),
            )
        )
    return items


def _chinese_lookup(
    query: str,
    page: int,
    per_page: int,
    channels: Sequence[SearchChannel] | None = None,
    *,
    credentials: dict[str, str] | None = None,
    rerank: bool = False,
) -> WorksResponse:
    """中文文献检索：授权通道优先，未命中则开放元数据兜底（可开关关闭）。

    口径（spec「论文检索」）：
    - 授权通道命中 → 标注来源通道与授权依据，覆盖度按通道自述（不标 partial）；
    - 无可用通道或通道无命中 → 走开放元数据，结果标 coverage=partial、
      metadata_only=true，响应带 coverage_note；
    - 仍然无命中 → reason=not_indexed（附覆盖说明），**不返回**
      no_authorized_channel——那是全文级能力的原因码。

    rerank=True 时对开放元数据候选请模型复核相关性（授权通道结果不动：
    那是精确通道，不需要二次取舍）。
    """
    data = search_chinese(
        query, page=page, per_page=per_page, channels=channels, credentials=credentials
    )
    channel = str(data.get("channel") or "")
    basis = str(data.get("authorization_basis") or "")
    items = [
        _channel_item(row, channel=channel, basis=basis)
        for row in data.get("records") or []
    ]
    if items:
        return WorksResponse(results=items, total=data.get("total"), page=page, per_page=per_page)

    channel_reason = str(data.get("reason") or "not_indexed")
    channel_detail = str(data.get("detail") or "")

    if not _open_fallback_enabled():
        # 关闭兜底 = 回到旧口径（中文仅经授权通道），便于回滚
        reason = "no_authorized_channel" if channel_reason == "no_authorized_channel" else channel_reason
        return _empty(
            reason=reason,  # type: ignore[arg-type]
            detail=channel_detail or "中文文献无命中",
            page=page,
            per_page=per_page,
            total=data.get("total"),
        )

    fallback = search_open_metadata(
        query, page=page, per_page=per_page, client=_OPEN_METADATA_CLIENT
    )
    open_items = _open_metadata_items(fallback)
    if open_items:
        if rerank:
            open_items = _apply_rerank(open_items, query)
        return WorksResponse(
            results=open_items,
            total=fallback.total,
            page=page,
            per_page=per_page,
            coverage_note=COVERAGE_NOTE,
        )

    # 中文题名零命中：可能该主题的文献全著录为英文题名，换检索词再试一次
    translated = _translated_query(query)
    cross_detail = ""
    if translated:
        retry = search_open_metadata(
            translated, page=page, per_page=per_page, client=_OPEN_METADATA_CLIENT
        )
        retry_items = _open_metadata_items(retry)
        if retry_items:
            if rerank:
                # 相关性按用户原始检索词判定：译文只是查库用的手段，不是需求本身
                retry_items = _apply_rerank(retry_items, query)
            return WorksResponse(
                results=retry_items,
                total=retry.total,
                page=page,
                per_page=per_page,
                coverage_note=COVERAGE_NOTE_CROSSLINGUAL,
            )
        cross_detail = f"英文检索词 {translated!r} 亦无命中"

    # 都查不到：把「授权通道为什么没给结果」与「开放元数据为什么没给结果」一起说清
    detail = "；".join(
        part
        for part in (
            f"授权通道：{channel_detail}" if channel_detail else "",
            f"开放元数据：{fallback.detail}" if fallback.detail else "",
            cross_detail,
        )
        if part
    )
    return _empty(
        reason="not_indexed",
        detail=f"{detail or '中文文献无命中'}（中文文献覆盖不完整）",
        page=page,
        per_page=per_page,
        total=fallback.total,
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
    channels: Sequence[SearchChannel] | None = None,
    credentials: dict[str, str] | None = None,
    rerank: bool = False,
) -> WorksResponse:
    """检索核心（无鉴权层，路由与真实验收脚本共用入口）。

    channels 仅供测试/演示注入中文授权适配器；None 时按
    FD_SHILUN_CN_CHANNELS 现读配置。
    credentials 是使用者自备的通道凭据（BYOK，优先级高于环境变量）；
    只在本次调用内使用，不落库、不进日志、不回显。
    rerank 只作用于中文兜底路径（开放元数据候选），其余路径忽略。
    """
    page = max(int(page), 1)
    per_page = min(max(int(per_page), 1), 25)
    doi = (doi or "").strip()
    if doi:  # DOI 精确取不受中文分流影响：DOI 一定走 OpenAlex
        return _doi_lookup(_new_provider(), _normalize_doi(doi))
    text = (query or "").strip()
    if _has_cjk(text):
        # 中文文献：授权通道优先，未命中则开放元数据兜底（spec「论文检索」）。
        return _chinese_lookup(
            text, page, per_page, channels, credentials=credentials, rerank=rerank
        )
    return _query_lookup(_new_provider(), text, page, per_page)



# ------------------------------------------------------------------- route

@router.post("/works", response_model=WorksResponse)
def works(body: WorksRequest, user_id: str = Depends(require_user)) -> WorksResponse:
    """论文检索：query（标题/关键词）或 doi（精确取）。空结果必带 reason。"""
    return search_works(
        query=body.query,
        doi=body.doi,
        page=body.page,
        per_page=body.per_page,
        credentials=body.channel_credentials,
        rerank=body.rerank,
    )
