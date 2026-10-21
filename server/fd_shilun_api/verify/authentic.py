"""二级「真实」核验：来源存在、元数据一致、未被撤稿。

判定必须可复现：同一份引用元数据与同一批上游记录，必得同一结论。为此
本模块只做确定性比对（归一化字符串相等），不引入任何模型判定。

三态口径（design 决策 2）：
- 记录存在但字段不一致 → block（确定性反证，给出不一致字段）；
- 记录被标记撤稿 → block；
- 通道明确答复却查无此条 → block「来源不存在」；
- 所有通道都失败/未响应 → pending_review（上游失联，标注待核，不当成假）；
- 记录一致 → pass，并注明是哪个来源、哪些字段吻合。

fulltext 只接受真正的原文；摘要与 OpenAlex 倒排索引一律不写入该字段——
三级核验只认原文，摘要冒充全文会让句级比对失真。
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Protocol
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator

from ..locator import Locator, citation_locator
from .models import (
    RULE_EXISTS,
    RULE_METADATA,
    RULE_RETRACTED,
    RULE_UPSTREAM,
    Evidence,
    VerificationItem,
)

# 逐字段比对的范围：spec 点名的作者/年份/标题/卷期页。
_COMPARE_FIELDS = ("title", "first_author", "year", "volume", "issue", "pages")

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_CROSSREF_DEFAULT = "https://api.crossref.org/works"
_OPENALEX_DEFAULT = "https://api.openalex.org/works"
_DATACITE_DEFAULT = "https://api.datacite.org/dois"
_PUBMED_DEFAULT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

_EVIDENCE_SOURCES = {"crossref", "openalex", "datacite", "pubmed"}


# --------------------------------------------------------------------------
# 数据契约
# --------------------------------------------------------------------------


class MetadataRecord(BaseModel):
    """上游返回的一条文献记录（已归一为与引用 meta 可比的形状）。"""

    source: str
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    retracted: bool = False
    url: str | None = None
    # 仅真正的原文全文；摘要不得写进来（规范：摘要不算全文）。
    fulltext: str | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _coerce_title(cls, v: Any) -> str:
        return "" if v is None else str(v).strip()

    @field_validator("authors", mode="before")
    @classmethod
    def _coerce_authors(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return [str(v).strip()]

    @field_validator("volume", "issue", "pages", "doi", "url", "fulltext", mode="before")
    @classmethod
    def _coerce_optional_str(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("year", mode="before")
    @classmethod
    def _coerce_year(cls, v: Any) -> int | None:
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        m = re.search(r"\d{4}", str(v))
        return int(m.group()) if m else None


class LookupResult(BaseModel):
    """一次查询的答复：命中记录、答复成功的通道、失败通道及原因。"""

    records: list[MetadataRecord] = Field(default_factory=list)
    # 答复成功（含明确「无此条」）的通道名，决定「来源不存在」能否成立。
    responded: list[str] = Field(default_factory=list)
    # 形如 "crossref: timeout"；全部失败时下游据此标 pending。
    failures: list[str] = Field(default_factory=list)


class MetadataResolver(Protocol):
    """元数据解析器契约：测试可注入确定性替身，生产走公开元数据接口。"""

    def lookup(self, meta: dict) -> LookupResult: ...


# --------------------------------------------------------------------------
# 归一化与逐字段比对
# --------------------------------------------------------------------------


def _norm_doi(raw: Any) -> str:
    doi = str(raw or "").strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)\s*", "", doi, flags=re.I)
    return doi.casefold()


def _norm_title(raw: Any) -> str:
    """标题比对：大小写、标点、空白、HTML 标签都不计入差异。"""
    s = unicodedata.normalize("NFKC", str(raw or ""))
    s = _HTML_TAG_RE.sub("", s).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s)


def _norm_serial(raw: Any) -> str:
    """卷/期号归一：去掉 Vol./No./Issue 前缀与无意义空白。"""
    s = unicodedata.normalize("NFKC", str(raw or "")).strip().casefold()
    s = re.sub(r"^(vol(?:ume)?|no|issue|iss|nr)\.?\s*", "", s)
    s = re.sub(r"\s+", "", s)
    return s.strip(".,;:()[]")


def _norm_pages(raw: Any) -> str:
    """页码归一：连字符变体（-、–、—、−）视为同一字符。"""
    s = unicodedata.normalize("NFKC", str(raw or "")).strip().casefold()
    s = re.sub(r"^(pp?|pages?)\.?\s*", "", s)
    for dash in ("\u2013", "\u2014", "\u2212"):
        s = s.replace(dash, "-")
    s = re.sub(r"\s+", "", s)
    return s.strip(".,;:")


def _as_year(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    m = re.search(r"\d{4}", str(raw))
    return int(m.group()) if m else None


def _first_author(meta: dict) -> str:
    raw = meta.get("authors") or meta.get("author")
    if isinstance(raw, str):
        return re.split(r"[;；]", raw)[0].strip()
    if isinstance(raw, (list, tuple)) and raw:
        return str(raw[0]).strip()
    return ""


def _surname(name: str) -> str:
    """取姓氏用于首作者比对。

    中文姓名整体比较（无法可靠切分复姓/双字名）；拉丁姓名按仓库既有约定
    「末段为姓」，但 "Shen Z" 这类缩写形式取首段，避免把缩写当成姓。
    """
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    if not s:
        return ""
    if _CJK_RE.search(s):
        return s.casefold()
    if "," in s:
        return s.split(",", 1)[0].strip().casefold()
    parts = s.split()
    if len(parts) == 1:
        return parts[0].strip(".").casefold()
    tail = parts[-1]
    tail_core = tail.replace(".", "")
    initial_like = 0 < len(tail_core) <= 2 and tail_core.isalpha() and tail_core.upper() == tail_core
    if initial_like:
        return parts[0].strip(".").casefold()
    return tail.strip(".").casefold()


def _compare(meta: dict, rec: MetadataRecord) -> tuple[dict[str, Any], list[str]]:
    """逐字段比对，返回 (不一致字段, 吻合字段)。

    只比对两边都有的字段：缺字段意味着「无从判断」，绝不能据此判为不符。
    """
    diffs: dict[str, Any] = {}
    matched: list[str] = []

    meta_title = str(meta.get("title") or "").strip()
    if meta_title and rec.title:
        if _norm_title(meta_title) == _norm_title(rec.title):
            matched.append("title")
        else:
            diffs["title"] = {"expected": meta_title, "found": rec.title}

    meta_author = _first_author(meta)
    rec_author = rec.authors[0] if rec.authors else ""
    if meta_author and rec_author:
        meta_surname, rec_surname = _surname(meta_author), _surname(rec_author)
        if meta_surname and rec_surname:
            if meta_surname == rec_surname:
                matched.append("first_author")
            else:
                diffs["first_author"] = {"expected": meta_author, "found": rec_author}

    meta_year = _as_year(meta.get("year"))
    if meta_year is not None and rec.year is not None:
        if meta_year == rec.year:
            matched.append("year")
        else:
            diffs["year"] = {"expected": meta.get("year"), "found": rec.year}

    for field, normalizer in (
        ("volume", _norm_serial),
        ("issue", _norm_serial),
        ("pages", _norm_pages),
    ):
        meta_value = str(meta.get(field) or "").strip()
        rec_value = getattr(rec, field)
        if meta_value and rec_value:
            if normalizer(meta_value) == normalizer(rec_value):
                matched.append(field)
            else:
                diffs[field] = {"expected": meta.get(field), "found": rec_value}

    # DOI 只作佐证，不参与拦截：spec 列明的拦截字段是作者、年份、标题、卷期页。
    meta_doi, rec_doi = _norm_doi(meta.get("doi")), _norm_doi(rec.doi)
    if meta_doi and rec_doi and meta_doi == rec_doi and "doi" not in matched:
        matched.append("doi")

    return diffs, matched


def _evidence_source(source: str) -> str:
    """未知来源降级为 rule，保证 Evidence.source 始终是合法枚举值。"""
    s = str(source or "").strip().lower()
    return s if s in _EVIDENCE_SOURCES else "rule"


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------


def _base_item(
    *,
    status: str,
    rule_id: str,
    locator: Locator,
    evidence: list[Evidence],
    reason: str,
) -> VerificationItem:
    return VerificationItem(
        rule_id=rule_id,
        level="authentic",
        status=status,  # type: ignore[arg-type]
        locator=locator,
        evidence=evidence,
        reason=reason,
    )


def verify_authentic(
    meta: dict,
    resolver: MetadataResolver,
    *,
    citation_id: str | None = None,
    locator: Locator | None = None,
) -> list[VerificationItem]:
    """对一条引用做二级核验，返回单条结论（列表形态便于逐级汇总）。"""
    meta = meta or {}
    cid = citation_id if citation_id is not None else str(meta.get("citation_id") or "")
    loc = locator or citation_locator(cid)

    try:
        result = resolver.lookup(meta)
    except Exception as exc:  # noqa: BLE001 —— 解析器崩溃同样属于「无法确定」
        result = LookupResult(failures=[f"resolver: {type(exc).__name__}: {exc}"])
    records = list(result.records or [])
    responded = [str(x) for x in (result.responded or [])]
    failures = [str(x) for x in (result.failures or [])]

    # 撤稿是比字段差异更明确的确定性反证，优先给出该结论。
    retracted = next((r for r in records if r.retracted), None)
    if retracted is not None:
        source = _evidence_source(retracted.source)
        return [
            _base_item(
                status="block",
                rule_id=RULE_RETRACTED,
                locator=loc,
                evidence=[
                    Evidence(
                        source=source,  # type: ignore[arg-type]
                        detail=f"{retracted.source} 标记该文献已撤稿",
                        fields={
                            "retracted": True,
                            "title": retracted.title,
                            "doi": retracted.doi,
                        },
                        url=retracted.url,
                    )
                ],
                reason="来源已撤稿，引用被拦截",
            )
        ]

    comparisons = [(rec, *_compare(meta, rec)) for rec in records]

    # 先找字段全吻合且有实际比对内容的记录；否则取差异最少的一条作证据。
    consistent = next(
        ((rec, matched) for rec, diffs, matched in comparisons if not diffs and matched),
        None,
    )
    if consistent is not None:
        rec, matched = consistent
        return [
            _base_item(
                status="pass",
                rule_id=RULE_METADATA,
                locator=loc,
                evidence=[
                    Evidence(
                        source=_evidence_source(rec.source),  # type: ignore[arg-type]
                        detail=f"{rec.source} 元数据一致，吻合字段：{', '.join(matched)}",
                        fields={"matched_fields": matched, "source": rec.source},
                        url=rec.url,
                    )
                ],
                reason="来源存在且元数据一致",
            )
        ]

    if comparisons:
        rec, diffs, matched = min(comparisons, key=lambda c: len(c[1]))
        if diffs:
            return [
                _base_item(
                    status="block",
                    rule_id=RULE_METADATA,
                    locator=loc,
                    evidence=[
                        Evidence(
                            source=_evidence_source(rec.source),  # type: ignore[arg-type]
                            detail=(
                                f"{rec.source} 元数据与引用不符，不一致字段："
                                f"{', '.join(diffs)}"
                            ),
                            fields=diffs,
                            url=rec.url,
                        )
                    ],
                    reason="元数据不符，引用被拦截",
                )
            ]
        # 有记录但两边的可比较字段为空：无法确认一致，按「无法确定」处理。
        return [
            _base_item(
                status="pending_review",
                rule_id=RULE_UPSTREAM,
                locator=loc,
                evidence=[
                    Evidence(
                        source=_evidence_source(rec.source),  # type: ignore[arg-type]
                        detail="来源有记录，但引用 meta 缺少可逐字段比对的字段",
                        fields={"matched_fields": matched, "source": rec.source},
                        url=rec.url,
                    )
                ],
                reason="引用元数据缺少可比对字段，无法完成真实性核验，标注待核",
            )
        ]

    if responded:
        return [
            _base_item(
                status="block",
                rule_id=RULE_EXISTS,
                locator=loc,
                evidence=[
                    Evidence(
                        source=_evidence_source(channel),  # type: ignore[arg-type]
                        detail="通道明确答复但无命中记录",
                        fields={"channel": channel, "responded": responded},
                    )
                    for channel in responded
                ],
                reason="来源不存在",
            )
        ]

    failure_text = "；".join(failures) if failures else "所有元数据通道均未响应"
    return [
        _base_item(
            status="pending_review",
            rule_id=RULE_UPSTREAM,
            locator=loc,
            evidence=[
                Evidence(
                    source="none",
                    detail=f"元数据通道不可用：{failure_text}",
                    fields={"failures": failures, "responded": responded},
                )
            ],
            reason=f"上游不可用，无法完成真实性核验：{failure_text}",
        )
    ]


# --------------------------------------------------------------------------
# 公开元数据通道（Crossref / OpenAlex / DataCite / PubMed）
# --------------------------------------------------------------------------


def _offline() -> bool:
    raw = (os.environ.get("FD_SHILUN_VERIFY_OFFLINE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _base(env_name: str, default: str) -> str:
    raw = (os.environ.get(env_name) or "").strip()
    return (raw or default).rstrip("/")


def _pubmed_base() -> str:
    raw = (os.environ.get("FD_SHILUN_PUBMED_URL") or "").strip() or _PUBMED_DEFAULT
    raw = re.sub(r"/(esearch|esummary)\.fcgi$", "", raw.rstrip("/"))
    return raw.rstrip("/")


def _first_str(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _first_str(item)
            if text:
                return text
        return ""
    return str(value).strip() if value is not None else ""


def _build_query(meta: dict) -> dict[str, str]:
    doi = _norm_doi(meta.get("doi"))
    title = str(meta.get("title") or "").strip()
    surname = _surname(_first_author(meta))
    text = " ".join(part for part in (title, surname) if part).strip()
    return {"doi": doi, "text": text}


def _short_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    return text[:160]


class HttpMetadataResolver:
    """走公开元数据接口的解析器；短超时 + 逐通道失败隔离。

    任一通道异常只记入 failures，其余通道继续；全部失败时上层据此标
    pending_review，而不是把「查不到」误判为「不存在」。
    """

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout

    def lookup(self, meta: dict) -> LookupResult:
        # 离线开关用于测试与演练：不发任何网络请求，也不假装通道答复过。
        if _offline():
            return LookupResult()
        query = _build_query(meta or {})
        if not query["doi"] and not query["text"]:
            return LookupResult(failures=["citation: 缺少 DOI 与标题，无法向元数据通道查询"])
        try:
            import httpx  # 延迟导入：未安装时降级为上游不可用，而不是导入即崩
        except Exception as exc:  # noqa: BLE001
            return LookupResult(failures=[f"httpx: 不可用（{_short_error(exc)}）"])

        records: list[MetadataRecord] = []
        responded: list[str] = []
        failures: list[str] = []
        client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        try:
            for channel, func in (
                ("crossref", self._crossref),
                ("openalex", self._openalex),
                ("datacite", self._datacite),
                ("pubmed", self._pubmed),
            ):
                try:
                    found = func(client, query)
                except Exception as exc:  # noqa: BLE001 —— 单通道失败不拖垮整体
                    failures.append(f"{channel}: {_short_error(exc)}")
                    continue
                responded.append(channel)
                records.extend(found)
        finally:
            client.close()
        return LookupResult(records=records, responded=responded, failures=failures)

    # ---- Crossref ----

    def _crossref(self, client: Any, query: dict[str, str]) -> list[MetadataRecord]:
        base = _base("FD_SHILUN_CROSSREF_URL", _CROSSREF_DEFAULT)
        items: list[Any] = []
        if query["doi"]:
            resp = client.get(f"{base}/{quote(query['doi'], safe='/')}")
            if resp.status_code == 404:
                resp = None
            elif resp.status_code >= 400:
                resp.raise_for_status()
            if resp is not None:
                message = (resp.json() or {}).get("message") or {}
                if message:
                    items = [message]
        if not items and query["text"]:
            resp = client.get(
                base, params={"query.bibliographic": query["text"], "rows": 5}
            )
            resp.raise_for_status()
            items = ((resp.json() or {}).get("message") or {}).get("items") or []
        return [
            rec
            for rec in (_crossref_record(item) for item in items)
            if rec is not None
        ]

    # ---- OpenAlex ----

    def _openalex(self, client: Any, query: dict[str, str]) -> list[MetadataRecord]:
        base = _base("FD_SHILUN_OPENALEX_URL", _OPENALEX_DEFAULT)
        results: list[Any] = []
        if query["doi"]:
            resp = client.get(base, params={"filter": f"doi:{query['doi']}", "per-page": 5})
            resp.raise_for_status()
            results = (resp.json() or {}).get("results") or []
        if not results and query["text"]:
            resp = client.get(base, params={"search": query["text"], "per-page": 5})
            resp.raise_for_status()
            results = (resp.json() or {}).get("results") or []
        return [
            rec
            for rec in (_openalex_record(item) for item in results)
            if rec is not None
        ]

    # ---- DataCite ----

    def _datacite(self, client: Any, query: dict[str, str]) -> list[MetadataRecord]:
        base = _base("FD_SHILUN_DATACITE_URL", _DATACITE_DEFAULT)
        items: list[Any] = []
        if query["doi"]:
            resp = client.get(f"{base}/{quote(query['doi'], safe='/')}")
            if resp.status_code == 404:
                resp = None
            elif resp.status_code >= 400:
                resp.raise_for_status()
            if resp is not None:
                data = (resp.json() or {}).get("data")
                if data:
                    items = [data]
        if not items and query["text"]:
            resp = client.get(base, params={"query": query["text"], "page[size]": 5})
            resp.raise_for_status()
            items = (resp.json() or {}).get("data") or []
        return [
            rec
            for rec in (_datacite_record(item) for item in items)
            if rec is not None
        ]

    # ---- PubMed ----

    def _pubmed(self, client: Any, query: dict[str, str]) -> list[MetadataRecord]:
        base = _pubmed_base()
        ids: list[str] = []
        if query["doi"]:
            ids = self._pubmed_ids(client, base, f"{query['doi']}[doi]")
        if not ids and query["text"]:
            ids = self._pubmed_ids(client, base, query["text"])
        if not ids:
            return []
        resp = client.get(
            f"{base}/esummary.fcgi",
            params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
        )
        resp.raise_for_status()
        result = (resp.json() or {}).get("result") or {}
        records: list[MetadataRecord] = []
        for uid in result.get("uids") or []:
            item = result.get(str(uid)) or {}
            rec = _pubmed_record(item)
            if rec is not None:
                records.append(rec)
        return records

    def _pubmed_ids(self, client: Any, base: str, term: str) -> list[str]:
        resp = client.get(
            f"{base}/esearch.fcgi",
            params={"db": "pubmed", "term": term, "retmode": "json", "retmax": 5},
        )
        resp.raise_for_status()
        payload = (resp.json() or {}).get("esearchresult") or {}
        return [str(x) for x in payload.get("idlist") or []]


def _crossref_retracted(item: dict) -> bool:
    for update in item.get("update-to") or []:
        if isinstance(update, dict):
            kind = str(update.get("type") or "").strip().lower()
            if kind in {"retraction", "withdrawal"}:
                return True
    relation = item.get("relation") or {}
    if isinstance(relation, dict):
        for key in ("is-retracted-by", "is-withdrawn-by"):
            if relation.get(key):
                return True
    return False


def _crossref_record(item: Any) -> MetadataRecord | None:
    if not isinstance(item, dict):
        return None
    title = _first_str(item.get("title"))
    if not title and not item.get("DOI"):
        return None
    authors: list[str] = []
    for author in item.get("author") or []:
        if isinstance(author, dict):
            name = " ".join(
                part for part in (author.get("given"), author.get("family")) if part
            ).strip()
            if name:
                authors.append(name)
        elif author:
            authors.append(str(author))
    year = None
    parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    if parts:
        year = parts[0]
    return MetadataRecord(
        source="crossref",
        title=title,
        authors=authors,
        year=year,
        volume=item.get("volume"),
        issue=item.get("issue"),
        pages=item.get("page"),
        doi=_norm_doi(item.get("DOI")),
        retracted=_crossref_retracted(item),
        url=item.get("URL"),
    )


def _openalex_record(item: Any) -> MetadataRecord | None:
    if not isinstance(item, dict):
        return None
    title = str(item.get("display_name") or item.get("title") or "").strip()
    if not title and not item.get("doi"):
        return None
    authors: list[str] = []
    for authorship in item.get("authorships") or []:
        if isinstance(authorship, dict):
            author = authorship.get("author") or {}
            if isinstance(author, dict) and author.get("display_name"):
                authors.append(str(author["display_name"]))
    biblio = item.get("biblio") or {}
    first_page = biblio.get("first_page") or ""
    last_page = biblio.get("last_page") or ""
    pages = ""
    if first_page and last_page:
        pages = f"{first_page}-{last_page}"
    elif first_page:
        pages = str(first_page)
    primary = item.get("primary_location") or {}
    return MetadataRecord(
        source="openalex",
        title=title,
        authors=authors,
        year=item.get("publication_year"),
        volume=biblio.get("volume"),
        issue=biblio.get("issue"),
        pages=pages or None,
        doi=_norm_doi(item.get("doi")),
        retracted=bool(item.get("is_retracted")),
        url=primary.get("landing_page_url") if isinstance(primary, dict) else None,
    )


def _datacite_record(item: Any) -> MetadataRecord | None:
    if not isinstance(item, dict):
        return None
    attrs = item.get("attributes") or {}
    if not isinstance(attrs, dict):
        return None
    title = _first_str(
        [
            t.get("title") if isinstance(t, dict) else t
            for t in attrs.get("titles") or []
        ]
    )
    if not title and not attrs.get("doi"):
        return None
    authors: list[str] = []
    for creator in attrs.get("creators") or []:
        if isinstance(creator, dict):
            name = str(creator.get("name") or "").strip() or " ".join(
                part
                for part in (creator.get("givenName"), creator.get("familyName"))
                if part
            ).strip()
            if name:
                authors.append(name)
    return MetadataRecord(
        source="datacite",
        title=title,
        authors=authors,
        year=attrs.get("publicationYear"),
        volume=attrs.get("volume"),
        issue=attrs.get("issue"),
        pages=attrs.get("page") or attrs.get("pages"),
        doi=_norm_doi(attrs.get("doi")),
        retracted=bool(attrs.get("isRetracted") or attrs.get("retracted")),
        url=attrs.get("url"),
    )


def _pubmed_record(item: dict) -> MetadataRecord | None:
    title = str(item.get("title") or "").strip()
    uid = str(item.get("uid") or "").strip()
    if not title:
        return None
    authors: list[str] = []
    for author in item.get("authors") or []:
        name = author.get("name") if isinstance(author, dict) else author
        if name:
            authors.append(str(name))
    year = None
    match = re.search(r"\d{4}", str(item.get("pubdate") or ""))
    if match:
        year = int(match.group())
    doi = None
    for article_id in item.get("articleids") or []:
        if isinstance(article_id, dict) and article_id.get("idtype") == "doi":
            doi = article_id.get("value")
            break
    pubtypes = [str(t).lower() for t in item.get("pubtype") or []]
    # 「Retracted Publication」才是被撤稿的原文；「Retraction of Publication」
    # 是撤稿通告本身，不能据此把被引原文判为撤稿。
    retracted = any("retracted publication" in t for t in pubtypes)
    return MetadataRecord(
        source="pubmed",
        title=title.rstrip("."),
        authors=authors,
        year=year,
        volume=item.get("volume"),
        issue=item.get("issue"),
        pages=item.get("pages"),
        doi=doi,
        retracted=retracted,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/" if uid else None,
    )
