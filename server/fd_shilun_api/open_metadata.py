"""开放元数据检索兜底（中文文献）：Crossref 与 OpenAlex 的公开查询。

为什么需要这一层：中文期刊凡注册了 DOI 的，其元数据就在 Crossref/OpenAlex 里；
未配置商业授权通道时，把这些条目当成「查不到」是不必要的损失（见 proposal.md）。

边界（不可越）：
- 只调用公开元数据 API，不抓取任何文献库页面，不驱动浏览器；
- 只返回**元数据**，不提供全文——需要原文的能力（三级「支持」核验）仍只认显式
  fulltext，拿不到就标待核；
- 覆盖不完整是事实，必须由调用方在结果上标注（`coverage=partial`），
  本模块不假装完整。

HTTP 约定沿用 `verify/authentic.py`：`FD_SHILUN_CROSSREF_URL` /
`FD_SHILUN_OPENALEX_URL` 覆盖基址，`FD_SHILUN_VERIFY_OFFLINE=1` 时不发任何请求
（返回 unreachable），测试用 `httpx.MockTransport` 注入。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote

CROSSREF_URL_ENV = "FD_SHILUN_CROSSREF_URL"
OPENALEX_URL_ENV = "FD_SHILUN_OPENALEX_URL"
OFFLINE_ENV = "FD_SHILUN_VERIFY_OFFLINE"

_CROSSREF_DEFAULT = "https://api.crossref.org/works"
_OPENALEX_DEFAULT = "https://api.openalex.org/works"
_TIMEOUT = 10.0

# 失败归因与授权通道、OpenAlex 主路径共用一套词汇，调用方不必学两套。
Failure = str  # quota | upstream | unreachable | not_indexed

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[a-z0-9]+")

# 相关性门（确定性、无 LLM）：查询词项在记录文本里的「覆盖率」低于此值即丢弃。
#
# 为什么必须有：Crossref 的 query.bibliographic 是 OR 语义——查「关于量子纠缠的
# 县域治理研究」会返回一堆量子物理论文（实测 total 57 万），OpenAlex 的 search
# 同样宽松。不过滤就会把「查不到」说成「查到了」，且 not_indexed 永不可达。
#
# 阈值取 0.3（与 verify/support.py 的 0.34 同源思路，这里略松以免过度过滤）：
# 长标题查询里真实相关文献的覆盖率通常 ≥0.3，纯噪声在 0.25 上下。
_RELEVANCE_THRESHOLD = 0.3
_MIN_MATCHED_TERMS = 2
# 无汉字记录（英文题名等）的豁免线：相对「通过词面门的最佳记录分数」的比值。
_NON_CJK_SCORE_RATIO = 0.8
# 过滤会砍掉大部分候选，所以向源多取几倍再截断到 per_page——否则小 per_page 时
# 「通过词面门的那条」可能根本没进候选，锚点消失导致整批被判不相关。
_OVERFETCH = 3


def _offline() -> bool:
    return os.environ.get(OFFLINE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _base(env: str, default: str) -> str:
    return (os.environ.get(env, "").strip() or default).rstrip("/")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = _WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", str(value))).strip()
    return text or None


@dataclass
class OpenMetadataResult:
    """一次开放元数据查询的答复（与通道适配器的结果形状对齐）。"""

    records: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    failure: Failure | None = None
    detail: str | None = None
    source: str = "open_metadata"


def _query_terms(query: str) -> tuple[set[str], set[str], set[str]]:
    """查询词项 → (CJK 二元组, 拉丁词, CJK 三元组)。

    二元组用于覆盖率，三元组用于「有没有实质共同词」：中文里两字相撞太容易
    （关于/研究/的X），三字起才算同一个术语（物权编/乡村振兴/医学影像）。
    """
    cjk: set[str] = set()
    latin: set[str] = set()
    trigrams: set[str] = set()
    for segment in _WS_RE.split(query or ""):
        for run in _CJK_RE.findall(segment):
            if len(run) == 1:
                cjk.add(run)
            else:
                cjk.update(run[i : i + 2] for i in range(len(run) - 1))
            trigrams.update(run[i : i + 3] for i in range(len(run) - 2))
        latin.update(_LATIN_RE.findall(segment.lower()))
    return cjk, latin, trigrams


def _relevant(
    record: Mapping[str, Any], cjk: set[str], latin: set[str], trigrams: set[str]
) -> bool:
    """确定性相关性判定：二元组覆盖率 + 三字术语覆盖率都要达标。

    三字术语要求**成比例覆盖**而非「撞上一个就够」：只撞一个三字词也可能只是
    顺带提及。实测四组样本的三字覆盖率——无关论文 0.00，三篇相关论文
    0.33 / 0.50 / 0.36——比例门槛能把「顺带提到一个术语」与「整条查询都在
    讲这件事」分开。
    """
    haystack = " ".join(
        str(record.get(key) or "") for key in ("title", "abstract", "venue")
    )
    if trigrams:
        shared = sum(1 for term in trigrams if term in haystack)
        # 三字词项太少时比例不稳定（0/1、1/1），退回「至少共享一个」
        if len(trigrams) < 3:
            if shared == 0:
                return False
        elif shared / len(trigrams) < _RELEVANCE_THRESHOLD:
            return False
    matched = 0
    total = 0
    for term in cjk:
        total += 1
        if term in haystack:
            matched += 1
    lowered = haystack.lower()
    for term in latin:
        total += 1
        if term in lowered:
            matched += 1
    if total == 0:
        return True  # 没有可比词项（纯符号查询）时不拦
    if matched < min(_MIN_MATCHED_TERMS, total):
        return False
    return matched / total >= _RELEVANCE_THRESHOLD


def _annotate(record: dict[str, Any], cjk: set[str], latin: set[str]) -> None:
    """把「本条命中查询的哪些词」写进记录。

    部分覆盖是这类检索的固有事实：与其让用户以为返回的就是全部答案，不如逐条
    说明它讲的是查询的哪一部分。纯本地计算，不再增加任何请求。
    """
    haystack = " ".join(
        str(record.get(key) or "") for key in ("title", "abstract", "venue")
    )
    lowered = haystack.lower()
    hits = [t for t in cjk if t in haystack] + [t for t in latin if t in lowered]
    total = len(cjk) + len(latin)
    record["matched_terms"] = sorted(hits)[:8]
    record["coverage_ratio"] = round(len(hits) / total, 2) if total else None


def _has_cjk_text(record: Mapping[str, Any]) -> bool:
    """记录文本里有没有汉字——没有就没法用中文词项做词面比对。"""
    haystack = " ".join(
        str(record.get(key) or "") for key in ("title", "abstract", "venue")
    )
    return bool(_CJK_RE.search(haystack))


def _filter_relevant(
    records: list[dict[str, Any]], query: str
) -> tuple[list[dict[str, Any]], int]:
    """→ (相关记录, 被过滤条数)。

    两档判定：
    1. **有汉字的记录**：查询词项覆盖率达标才留（词面证据）。
    2. **无汉字的记录**（英文/韩文题名等）：词面比对无从谈起，改用上游自己的
       相关度分数——只有它 ≥「通过第一档的最佳记录分数 × 0.8」才留。

    为什么第 2 档要锚定在第一档上：中英之间没有可比的字符串，单看英文记录的
    绝对分数分不清相关与无关（实测「关于量子纠缠的县域治理研究」的英文量子
    论文分数 59.1，与「民法典物权编」的英文相关论文 53.7 同量级）。但若该查询
    **一条汉字记录都没通过**，说明查询本身与这批结果不相关，此时不给英文记录
    豁免——无关查询因此仍然返回空，not_indexed 保持可达。
    """
    cjk_terms, latin_terms, trigrams = _query_terms(query)
    if not cjk_terms:
        # 纯拉丁查询（含跨语言补齐后的英文检索词）：不存在跨语言问题，
        # 所有记录都用词面覆盖率直接判定，不需要汉字记录做锚点。
        kept = [r for r in records if _relevant(r, cjk_terms, latin_terms, trigrams)]
        for record in kept:
            record.pop("score", None)
            _annotate(record, cjk_terms, latin_terms)
        return kept, len(records) - len(kept)

    kept_ids: set[int] = set()
    anchor = 0.0
    for record in records:
        if _has_cjk_text(record) and _relevant(record, cjk_terms, latin_terms, trigrams):
            kept_ids.add(id(record))
            anchor = max(anchor, float(record.get("score") or 0))

    if kept_ids and anchor > 0:
        for record in records:
            if id(record) in kept_ids or _has_cjk_text(record):
                continue
            if float(record.get("score") or 0) >= _NON_CJK_SCORE_RATIO * anchor:
                kept_ids.add(id(record))

    kept = [r for r in records if id(r) in kept_ids]
    # score 只是本层的判定输入，不进对外的记录契约
    for record in kept:
        record.pop("score", None)
        _annotate(record, cjk_terms, latin_terms)
    return kept, len(records) - len(kept)


def _crossref_record(item: Mapping[str, Any]) -> dict[str, Any]:
    authors: list[str] = []
    for author in item.get("author") or []:
        if not isinstance(author, Mapping):
            continue
        name = " ".join(
            str(part).strip()
            for part in (author.get("given"), author.get("family"))
            if str(part or "").strip()
        )
        if name:
            authors.append(name)
    issued = item.get("issued") or item.get("published") or {}
    parts = issued.get("date-parts") if isinstance(issued, Mapping) else None
    year = None
    if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
        try:
            year = int(parts[0][0])
        except (TypeError, ValueError):
            year = None
    return {
        "stable_id": str(item.get("DOI") or ""),
        "doi": item.get("DOI"),
        "title": _clean((item.get("title") or [None])[0]),
        "authors": authors,
        "year": year,
        "venue": _clean((item.get("container-title") or [None])[0]),
        "abstract": _clean(item.get("abstract")),
        "url": item.get("URL"),
        # 上游相关度分数：仅用于「无汉字记录」的第二档判定，不对外暴露
        "score": item.get("score"),
    }


def _openalex_record(item: Mapping[str, Any]) -> dict[str, Any]:
    authors: list[str] = []
    for authorship in item.get("authorships") or []:
        if isinstance(authorship, Mapping):
            name = _clean((authorship.get("author") or {}).get("display_name"))
            if name:
                authors.append(name)
    location = item.get("primary_location") or {}
    venue = None
    if isinstance(location, Mapping):
        source = location.get("source")
        if isinstance(source, Mapping):
            venue = _clean(source.get("display_name"))
    return {
        "stable_id": str(item.get("id") or "").rstrip("/").split("/")[-1],
        "doi": item.get("doi"),
        "title": _clean(item.get("title") or item.get("display_name")),
        "authors": authors,
        "year": item.get("publication_year"),
        "venue": venue,
        "abstract": None,  # OpenAlex 的摘要是指数倒排，不还原成「原文」
        "url": item.get("id"),
        "score": item.get("relevance_score"),
    }


def _post(url: str, params: dict[str, str], *, client: Any) -> tuple[dict | None, str | None]:
    """一次 GET；返回 (payload, failure)。任何异常都归一为可读归因。"""
    if _offline():
        return None, "unreachable"
    if client is not None:
        try:
            resp = client.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json(), None
        except Exception as exc:  # noqa: BLE001 —— 归因比抛栈有用
            return None, _classify(exc)
    try:
        import httpx
    except ImportError:  # pragma: no cover - 运行环境缺 httpx
        return None, "unreachable"
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as http:
            resp = http.get(url, params=params, headers={"User-Agent": "fd-shilun/0.1"})
            resp.raise_for_status()
            return resp.json(), None
    except Exception as exc:  # noqa: BLE001
        return None, _classify(exc)


def _classify(exc: BaseException) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return "quota"
    if isinstance(status, int) and status >= 500:
        return "upstream"
    name = type(exc).__name__
    if "Timeout" in name or "Connect" in name or isinstance(exc, OSError):
        return "unreachable"
    return "upstream"


def search_crossref(
    query: str, *, page: int = 1, per_page: int = 10, client: Any = None
) -> OpenMetadataResult:
    """Crossref 书目检索（query.bibliographic），返回结构化元数据。"""
    payload, failure = _post(
        _base(CROSSREF_URL_ENV, _CROSSREF_DEFAULT),
        {
            "query.bibliographic": query,
            "rows": str(min(per_page * _OVERFETCH, 100)),
            "offset": str((page - 1) * per_page * _OVERFETCH),
            # 不传 select：score 不是可选字段，而第二档相关性判定要用它
        },
        client=client,
    )
    if payload is None:
        return OpenMetadataResult(failure=failure, detail=f"Crossref 查询失败（{failure}）", source="crossref")
    message = payload.get("message") if isinstance(payload, Mapping) else None
    items = (message or {}).get("items") or []
    records = [_crossref_record(item) for item in items if isinstance(item, Mapping)]
    records = [r for r in records if r.get("title")]
    records, dropped = _filter_relevant(records, query)
    records = records[:per_page]
    total = (message or {}).get("total-results")
    if records:
        detail = None
    elif dropped:
        detail = f"Crossref 无相关命中（已过滤 {dropped} 条弱相关）"
    else:
        detail = "Crossref 无命中"
    return OpenMetadataResult(
        records=records,
        total=total if isinstance(total, int) else None,
        failure=None if records else "not_indexed",
        detail=detail,
        source="crossref",
    )


def search_openalex(
    query: str, *, page: int = 1, per_page: int = 10, client: Any = None
) -> OpenMetadataResult:
    """OpenAlex 关键词检索（与主路径同一公开接口，只是用于中文查询兜底）。"""
    payload, failure = _post(
        _base(OPENALEX_URL_ENV, _OPENALEX_DEFAULT),
        {
            "search": query,
            "page": str(page),
            "per-page": str(min(per_page * _OVERFETCH, 100)),
            # 不传 select：relevance_score 用于无汉字记录的第二档判定
        },
        client=client,
    )
    if payload is None:
        return OpenMetadataResult(failure=failure, detail=f"OpenAlex 查询失败（{failure}）", source="openalex")
    items = payload.get("results") or []
    records = [_openalex_record(item) for item in items if isinstance(item, Mapping)]
    records = [r for r in records if r.get("title")]
    records, dropped = _filter_relevant(records, query)
    records = records[:per_page]
    meta = payload.get("meta") or {}
    total = meta.get("count")
    if records:
        detail = None
    elif dropped:
        detail = f"OpenAlex 无相关命中（已过滤 {dropped} 条弱相关）"
    else:
        detail = "OpenAlex 无命中"
    return OpenMetadataResult(
        records=records,
        total=total if isinstance(total, int) else None,
        failure=None if records else "not_indexed",
        detail=detail,
        source="openalex",
    )


# 兜底顺序：先 Crossref（中文期刊 DOI 注册地更集中），再 OpenAlex（补充）。
# 两者都是公开元数据；任何一方命中即返回，并标注 coverage=partial。
_PROVIDERS = (search_crossref, search_openalex)


def search_open_metadata(
    query: str, *, page: int = 1, per_page: int = 10, client: Any = None
) -> OpenMetadataResult:
    """按顺序问公开元数据源，返回第一个有命中的结果。

    全部无命中时按「信息量」归因：任一源返回 quota/upstream/unreachable 优先于
    not_indexed——把「上游挂了」说成「查不到」会让用户白改检索词。
    """
    failures: list[tuple[str, str, str | None]] = []
    for provider in _PROVIDERS:
        result = provider(query, page=page, per_page=per_page, client=client)
        if result.records:
            return result
        failures.append((result.source, result.failure or "not_indexed", result.detail))

    priority = {"quota": 0, "upstream": 1, "unreachable": 2, "not_indexed": 3}
    source, failure, detail = min(failures, key=lambda f: priority.get(f[1], 9))
    return OpenMetadataResult(
        failure=failure,
        detail="；".join(f"{s}: {d or r}" for s, r, d in failures) or detail,
        source=source,
    )


__all__ = [
    "OpenMetadataResult",
    "search_crossref",
    "search_openalex",
    "search_open_metadata",
]
