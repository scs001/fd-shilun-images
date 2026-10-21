"""论文写作辅助（tasks 4.1/4.2/4.3）：引用清单 / 提纲锚定 / 引用可解校验。

- 存储：自包含 SQLite（env FD_SHILUN_DB，默认 ~/.fd-shilun/shilun.db，
  row_factory + WAL + 外键），幂等 CREATE IF NOT EXISTS 建表，随连接执行。
- 鉴权：全部路由 Depends(require_user)。require_user 延迟导入
  auth.authenticate（并行交付期 personal 模块可能尚未落盘；语义不变：
  无有效 Bearer 即 401，并由 auth 统一写审计留痕）。
- 引用可解（核心不变量，spec「严肃文本引用可解」）：稿件文本中的
  [[cite:<citation_id>]] 标记逐个对照本项目 citations 表；解不出的标记
  在校验输出的 cleaned_text 中剥除（宁删勿假）。
- 引用格式化：纯 python 实现 GB/T 7714-2015 与 APA 7 两套，期刊文章与
  预印本完整覆盖，其余类型优雅降级并显式标注（degraded）。
- 提纲：确定性引擎（收藏引用标题的词频 + 共现贪心聚类），每节标注
  依据 citation id，无依据章节标 needs_evidence:true；可选 LLM 增强
  （.llm 网关，无 KEY 跳过、失败降级，engine 字段如实标明；LLM 输出
  的 citation_ids 同样过「可解」闸——虚构引用一律剥除）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .llm import LLMError, chat, is_configured
from .verify.authentic import HttpMetadataResolver, MetadataResolver
from .verify.resolvable import CITE_MARKER_RE
from .verify.service import verification_mode, verify_text

# --------------------------------------------------------------------------
# 存储（自包含，参考 fd-zhesi personal.py 的连接模式）
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('thesis','govdoc')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_writing_projects_user ON projects(user_id, created_at);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    snapshot TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_writing_versions_project ON versions(project_id, created_at);
CREATE TABLE IF NOT EXISTS citations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('openalex','gov_corpus')),
    ref_id TEXT NOT NULL,
    meta TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_writing_citations_project ON citations(project_id, created_at);
"""


def db_path() -> str:
    return os.environ.get("FD_SHILUN_DB", "").strip() or str(
        Path.home() / ".fd-shilun" / "shilun.db"
    )


def connect(db_path_: str | None = None) -> sqlite3.Connection:
    path = db_path_ or db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)  # 幂等：已存在的表/索引不动
    conn.commit()
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------
# 鉴权依赖（延迟导入：personal 模块由并行任务交付，导入时机推迟到请求期）
# --------------------------------------------------------------------------


def require_user(request: Request) -> str:
    """等价 auth.require_user：走 auth.authenticate 统一判定并留痕。"""
    from .auth import authenticate

    return authenticate(request)


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------


class ProjectIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    kind: Literal["thesis", "govdoc"]


class VersionIn(BaseModel):
    snapshot: dict[str, Any] | list[Any] | str
    note: str | None = Field(default=None, max_length=500)


class CitationIn(BaseModel):
    kind: Literal["openalex", "gov_corpus"]
    ref_id: str = Field(min_length=1, max_length=500)
    meta: dict[str, Any] = Field(default_factory=dict)


class OutlineIn(BaseModel):
    use_llm: bool = True


class ValidateIn(BaseModel):
    text: str | None = None


# --------------------------------------------------------------------------
# 引用格式化（纯 python，GB/T 7714-2015 与 APA 7）
# --------------------------------------------------------------------------

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_JOURNAL_TYPES = {"journal-article", "article", "journal", "article-journal"}
_PREPRINT_TYPES = {"preprint", "arxiv", "working-paper", "posted-content"}


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s or ""))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _as_author_list(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [a.strip() for a in re.split(r"[;；]", raw) if a.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(a).strip() for a in raw if str(a).strip()]
    return [str(raw).strip()]


def _norm_type(meta: dict[str, Any]) -> str:
    """归一化文献类型：journal / preprint / 原始串（降级）。无类型时按字段推断。"""
    t = str(meta.get("type") or "").strip().lower()
    if t in _JOURNAL_TYPES:
        return "journal"
    if t in _PREPRINT_TYPES:
        return "preprint"
    if not t:
        if str(meta.get("venue") or "").strip():
            return "journal"
        if _preprint_url(meta):
            return "preprint"
        return "unknown"
    return t


def _norm_doi(raw: Any) -> str:
    doi = str(raw or "").strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)\s*", "", doi, flags=re.I)
    return doi


def _preprint_url(meta: dict[str, Any]) -> str:
    url = str(meta.get("url") or "").strip()
    if url:
        return url
    arxiv = str(meta.get("arxiv_id") or meta.get("arxiv") or "").strip()
    if arxiv:
        arxiv = re.sub(r"^arxiv:\s*", "", arxiv, flags=re.I)
        return f"https://arxiv.org/abs/{arxiv}"
    return ""


def _author_gbt(name: str) -> str:
    """GB/T 7714 作者：姓前名后缩写（Shen Zhihong → Shen Z）；中文姓名原样。"""
    name = name.strip()
    if not name or _has_cjk(name):
        return name
    parts = name.split()
    if len(parts) == 1:
        return name
    surname = parts[-1]
    initials = "".join(p[0].upper() for p in parts[:-1] if p)
    return f"{surname} {initials}" if initials else surname


def _authors_gbt(names: list[str]) -> str:
    """≤3 位全列；>3 位列前 3 位，中文加「等」、英文加「et al.」。"""
    if not names:
        return ""
    zh = any(_has_cjk(n) for n in names)
    if len(names) <= 3:
        return ", ".join(_author_gbt(n) for n in names)
    return ", ".join(_author_gbt(n) for n in names[:3]) + (", 等" if zh else ", et al.")


def _author_apa(name: str) -> str:
    """APA 7 作者：Surname, F. M.；中文姓名原样。"""
    name = name.strip()
    if not name or _has_cjk(name):
        return name
    parts = name.split()
    if len(parts) == 1:
        return name
    surname = parts[-1]
    initials = " ".join(f"{p[0].upper()}." for p in parts[:-1] if p)
    return f"{surname}, {initials}" if initials else surname


def _authors_apa(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return _author_apa(names[0])
    if len(names) <= 20:
        return ", ".join(_author_apa(n) for n in names[:-1]) + f", & {_author_apa(names[-1])}"
    # APA 7 的 21+ 位规则：前 19 位 + 省略号 + 最后一位
    return (
        ", ".join(_author_apa(n) for n in names[:19])
        + f", ... {_author_apa(names[-1])}"
    )


def format_gbt7714(meta: dict[str, Any]) -> tuple[str, bool]:
    """GB/T 7714-2015。返回 (文本, 是否降级)。期刊 [J] / 预印本 [EB/OL] 完整覆盖。"""
    meta = meta or {}
    typ = _norm_type(meta)
    title = str(meta.get("title") or "").strip()
    authors = _as_author_list(meta.get("authors"))
    year = str(meta.get("year") or "").strip()
    zh = _has_cjk(title) or any(_has_cjk(a) for a in authors)
    parts: list[str] = []

    a = _authors_gbt(authors)
    if a:
        parts.append(a)
    doc_tag = {"journal": "[J]", "preprint": "[EB/OL]"}.get(typ, "[Z]")
    if title:
        parts.append(f"{title}{doc_tag}")

    if typ == "journal":
        bits: list[str] = []
        venue = str(meta.get("venue") or "").strip()
        if venue:
            bits.append(venue)
        vol = str(meta.get("volume") or "").strip()
        iss = str(meta.get("issue") or "").strip()
        if year:
            bits.append(year + (f"({iss})" if iss and not vol else ""))
        if vol:
            bits.append(vol + (f"({iss})" if iss else ""))
        pages = str(meta.get("pages") or "").strip()
        if pages and bits:
            bits[-1] = f"{bits[-1]}: {pages}"
        elif pages:
            bits.append(pages)
        if bits:
            parts.append(", ".join(bits))
        doi = _norm_doi(meta.get("doi"))
        if doi:
            parts.append(f"DOI:{doi}")
    elif typ == "preprint":
        # GB/T 电子文献：发布日期紧挨引用日期，如 (2023)[2026-10-21]
        pub = str(meta.get("published") or year or "").strip()
        if pub:
            parts.append(f"({pub})[{_today()}]")
        else:
            parts.append(f"[{_today()}]")
        url = _preprint_url(meta)
        if url:
            parts.append(url)
        else:
            doi = _norm_doi(meta.get("doi"))
            if doi:
                parts.append(f"DOI:{doi}")
    else:
        if year:
            parts.append(year)
        url = str(meta.get("url") or "").strip()
        if url:
            parts.append(url)

    # 各段去尾点再以 “. ” 连接（“et al.”/作者缩写自带尾点时避免双点）
    text = ". ".join(p.rstrip(".").strip() for p in parts if p).strip()
    if text:
        text += "."
    degraded = typ not in ("journal", "preprint")
    if degraded and text:
        note = (
            f"（注：{typ} 类型未完整适配 GB/T 7714，已按通用格式降级输出）"
            if zh
            else f" [Note: type '{typ}' not fully covered by GB/T 7714; degraded to generic format]"
        )
        text += note
    return text, degraded


def format_apa(meta: dict[str, Any]) -> tuple[str, bool]:
    """APA 7。返回 (文本, 是否降级)。期刊与预印本完整覆盖。"""
    meta = meta or {}
    typ = _norm_type(meta)
    title = str(meta.get("title") or "").strip()
    authors = _as_author_list(meta.get("authors"))
    year = str(meta.get("year") or "").strip() or "n.d."
    segs: list[str] = []

    a = _authors_apa(authors)
    segs.append(f"{a.rstrip('.')}. ({year})." if a else f"({year}).")
    if title:
        segs.append(f"{title}.")

    if typ == "journal":
        tail: list[str] = []
        venue = str(meta.get("venue") or "").strip()
        if venue:
            tail.append(venue)
        vol = str(meta.get("volume") or "").strip()
        iss = str(meta.get("issue") or "").strip()
        if vol and iss:
            tail.append(f"{vol}({iss})")
        elif vol or iss:
            tail.append(vol or iss)
        pages = str(meta.get("pages") or "").strip()
        if pages:
            tail.append(pages)
        if tail:
            segs.append(", ".join(tail) + ".")
    elif typ == "preprint":
        url = _preprint_url(meta)
        arxivish = "arxiv" in url.lower() or str(meta.get("arxiv_id") or "").strip()
        platform = str(meta.get("repository") or "").strip() or ("arXiv" if arxivish else "Preprint")
        segs.append(f"{platform}.")

    doi = _norm_doi(meta.get("doi"))
    if doi:
        segs.append(f"https://doi.org/{doi}")
    elif typ == "preprint":
        url = _preprint_url(meta)
        if url:
            segs.append(url)
    elif typ not in ("journal", "preprint"):
        url = str(meta.get("url") or "").strip()
        if url:
            segs.append(url)

    text = " ".join(s for s in segs if s).strip()
    degraded = typ not in ("journal", "preprint")
    if degraded and text:
        zh = _has_cjk(title) or any(_has_cjk(x) for x in authors)
        note = (
            f"（注：{typ} 类型未完整适配 APA 7，已按通用格式降级输出）"
            if zh
            else f"[Note: type '{typ}' not fully covered by APA 7; degraded to generic format]"
        )
        text += (" " if not zh else "") + note
    return text, degraded


FORMATTERS = {"gbt7714": format_gbt7714, "apa": format_apa}


# --------------------------------------------------------------------------
# 提纲：确定性聚类（词频 + 共现）+ 可选 LLM 增强
# --------------------------------------------------------------------------

_EN_STOP = {
    "the", "a", "an", "and", "or", "of", "for", "with", "to", "in", "on", "by",
    "from", "as", "at", "is", "are", "was", "were", "be", "been", "this",
    "that", "these", "those", "their", "its", "we", "our", "us", "it", "based",
    "using", "via", "study", "paper", "research", "toward", "towards", "how",
    "can", "not", "new", "more", "most", "also", "than", "then", "when",
    "where", "which", "what", "who", "whose", "why", "into", "over", "under",
    "between", "within", "across", "during", "after", "before", "about",
    "against", "out", "up", "down", "off", "again", "once", "here", "there",
    "all", "any", "both", "each", "few", "other", "some", "such", "only",
    "own", "same", "so", "too", "very", "will", "would", "shall", "should",
    "may", "might", "must", "could", "does", "did", "doing", "done", "has",
    "have", "had", "having", "you", "your", "they", "them", "his", "her",
}
_ZH_STOP_CHARS = set("的了着呢吧啊嘛么与以及或者在对从把被其该此就也很更最们")


def _tokenize(text: str) -> list[str]:
    """标题分词：英文小写词（≥3 字符）+ 中文二元组；去停用词。"""
    out: list[str] = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) >= 3 and w not in _EN_STOP and not w.isdigit():
            out.append(w)
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if not (_ZH_STOP_CHARS & set(bg)):
                out.append(bg)
    return out


def _cluster_citations(rows: list[dict]) -> list[dict]:
    """贪心聚类：与某簇共享 ≥2 个主题词归入该簇，否则开新簇（保序、确定）。"""
    clusters: list[dict] = []
    for row in rows:
        toks = _tokenize(str(row["meta"].get("title") or ""))
        tset = set(toks)
        best: dict | None = None
        best_overlap = 0
        for cl in clusters:
            overlap = len(tset & set(cl["terms"]))
            if overlap > best_overlap:
                best, best_overlap = cl, overlap
        if best is not None and best_overlap >= 2:
            best["terms"].update(toks)
            best["rows"].append(row)
        else:
            clusters.append({"terms": Counter(toks), "rows": [row]})
    return clusters


def _basis_of(row: dict) -> dict:
    meta = row["meta"]
    return {
        "citation_id": row["id"],
        "ref_id": row["ref_id"],
        "title": meta.get("title"),
        "year": meta.get("year"),
    }


def _deterministic_outline(rows: list[dict], kind: str) -> list[dict]:
    structural = (
        ["发文缘由与依据", "执行要求与落款说明"]
        if kind == "govdoc"
        else ["引言：研究背景与问题提出", "讨论与结论"]
    )
    sections: list[dict] = [
        {
            "id": "sec_intro",
            "title": structural[0],
            "needs_evidence": True,
            "citation_ids": [],
            "basis": [],
        }
    ]
    for i, cl in enumerate(_cluster_citations(rows), start=1):
        top = [t for t, _ in sorted(cl["terms"].items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
        title = f"相关工作：{'，'.join(top)}" if top else "相关工作（主题待提炼）"
        sections.append(
            {
                "id": f"sec_{i}",
                "title": title,
                "needs_evidence": False,
                "citation_ids": [r["id"] for r in cl["rows"]],
                "basis": [_basis_of(r) for r in cl["rows"]],
            }
        )
    sections.append(
        {
            "id": "sec_conclusion",
            "title": structural[1],
            "needs_evidence": True,
            "citation_ids": [],
            "basis": [],
        }
    )
    return sections


def _llm_enhance(
    sections: list[dict], rows: list[dict], kind: str
) -> tuple[list[dict] | None, str]:
    """LLM 润色提纲。返回 (新提纲, 说明)；失败/未配置返回 (None, 原因)。

    输出必须过「可解」闸：citation_ids 只保留真实存在的 id，节内无有效
    依据则 needs_evidence:true；未被任何节引用的真实引用追加兜底节
    （证据宁全勿丢，引用宁删勿假）。
    """
    if not is_configured():
        return None, "FD_SHILUN_LLM_API_KEY 未配置，使用确定性引擎"
    valid_ids = {r["id"] for r in rows}
    prompt = (
        "你是学术写作提纲编辑，只输出 JSON，不要输出任何其他文字。\n"
        f"项目类型：{'thesis（论文）' if kind == 'thesis' else 'govdoc（公文）'}。\n"
        "下面是确定性聚类生成的提纲骨架与收藏引用列表。请优化章节标题、"
        "补充每节一句话要点（summary），可以增删合并章节，但必须遵守：\n"
        "1. 每节 citation_ids 只能使用引用列表中出现过的 citation_id，不得虚构；\n"
        "2. 没有依据的章节 citation_ids 为空数组且 needs_evidence 为 true；\n"
        '3. 输出格式：{"sections":[{"title":"...","summary":"...",'
        '"citation_ids":["..."],"needs_evidence":false}]}\n\n'
        f"确定性骨架：{json.dumps(sections, ensure_ascii=False)}\n"
        f"引用列表：{json.dumps([_basis_of(r) for r in rows], ensure_ascii=False)}"
    )
    try:
        raw = chat(
            [
                {"role": "system", "content": "只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1200,
        )
    except LLMError as exc:
        return None, f"LLM 网关失败，降级确定性引擎：{exc}"
    if not raw:
        return None, "LLM 返回为空，降级确定性引擎"
    try:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        data = json.loads(m.group(0)) if m else None
        raw_sections = data["sections"] if isinstance(data, dict) else None
        if not isinstance(raw_sections, list) or not raw_sections:
            raise ValueError("sections 为空")
    except Exception:  # noqa: BLE001 —— 任何解析失败都降级
        return None, "LLM 输出无法解析为提纲 JSON，降级确定性引擎"

    used: set[str] = set()
    out: list[dict] = []
    for i, sec in enumerate(raw_sections, start=1):
        if not isinstance(sec, dict):
            continue
        cids = [
            str(c) for c in (sec.get("citation_ids") or [])
            if str(c) in valid_ids
        ]
        cids = [c for c in dict.fromkeys(cids)]  # 去重保序
        used.update(cids)
        out.append(
            {
                "id": f"sec_{i}",
                "title": str(sec.get("title") or "").strip() or f"章节 {i}",
                "summary": str(sec.get("summary") or "").strip() or None,
                "needs_evidence": not cids,
                "citation_ids": cids,
                "basis": [next(_basis_of(r) for r in rows if r["id"] == c) for c in cids],
            }
        )
    leftover = [r for r in rows if r["id"] not in used]
    if leftover:
        out.append(
            {
                "id": f"sec_{len(out) + 1}",
                "title": "文献综述：待归位引用",
                "summary": None,
                "needs_evidence": False,
                "citation_ids": [r["id"] for r in leftover],
                "basis": [_basis_of(r) for r in leftover],
            }
        )
    return out, "LLM 增强"


# --------------------------------------------------------------------------
# 引用可解校验（核心不变量）
# --------------------------------------------------------------------------

# 元数据解析器在模块级可替换：测试注入确定性替身，生产走公开元数据通道。
# 单通道失败只会让该引用标待核，不影响校验主流程，更不会把「查不到」当「假」。
metadata_resolver: MetadataResolver = HttpMetadataResolver()


def _snapshot_text(snapshot: Any) -> str | None:
    """从版本快照 JSON 中取稿件文本：str 直取；dict 取 text/content/body。"""
    if isinstance(snapshot, str):
        return snapshot if snapshot.strip() else None
    if isinstance(snapshot, dict):
        for key in ("text", "content", "body"):
            v = snapshot.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return None


def resolve_citations(text: str, valid_ids: set[str]) -> dict:
    """逐个校验 [[cite:<id>]] 标记；解不出的在 cleaned_text 中剥除（宁删勿假）。"""
    found = CITE_MARKER_RE.findall(text)
    valid: list[str] = []
    stripped: list[str] = []
    for cid in dict.fromkeys(c.strip() for c in found):
        (valid if cid in valid_ids else stripped).append(cid)

    def _keep(m: re.Match) -> str:
        return m.group(0) if m.group(1).strip() in valid_ids else ""

    cleaned = CITE_MARKER_RE.sub(_keep, text)
    return {
        "marker_count": len(found),
        "valid": valid,
        "stripped": stripped,
        "cleaned_text": cleaned,
    }


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/writing", tags=["writing"])


def _project_or_404(conn: sqlite3.Connection, project_id: str, user_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, user_id)
    ).fetchone()
    if not row:
        raise HTTPException(404, "项目不存在或不属于当前用户")
    return row


def _project_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "user_id": r["user_id"],
        "title": r["title"],
        "kind": r["kind"],
        "created_at": r["created_at"],
    }


def _version_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "project_id": r["project_id"],
        "snapshot": json.loads(r["snapshot"]),
        "note": r["note"],
        "created_at": r["created_at"],
    }


def _citation_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "project_id": r["project_id"],
        "kind": r["kind"],
        "ref_id": r["ref_id"],
        "meta": json.loads(r["meta"]),
        "created_at": r["created_at"],
    }


def _list_citations(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM citations WHERE project_id=? ORDER BY created_at, id",
        (project_id,),
    ).fetchall()
    return [_citation_dict(r) for r in rows]


# ---- 1. 项目 CRUD + 版本快照 ----


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, user_id: str = Depends(require_user)) -> dict:
    pid = new_id()
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO projects (id, user_id, title, kind, created_at) VALUES (?,?,?,?,?)",
            (pid, user_id, body.title, body.kind, now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return _project_dict(row)


@router.get("/projects")
def list_projects(user_id: str = Depends(require_user)) -> dict:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM projects WHERE user_id=? ORDER BY created_at, id",
            (user_id,),
        ).fetchall()
        return {"count": len(rows), "results": [_project_dict(r) for r in rows]}


@router.post("/projects/{project_id}/versions", status_code=201)
def create_version(
    project_id: str, body: VersionIn, user_id: str = Depends(require_user)
) -> dict:
    vid = new_id()
    with closing(connect()) as conn:
        _project_or_404(conn, project_id, user_id)
        snapshot = (
            json.dumps(body.snapshot, ensure_ascii=False)
            if not isinstance(body.snapshot, str)
            else body.snapshot
        )
        conn.execute(
            "INSERT INTO versions (id, project_id, snapshot, note, created_at)"
            " VALUES (?,?,?,?,?)",
            (vid, project_id, snapshot, body.note, now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
        return _version_dict(row)


@router.get("/projects/{project_id}/versions")
def list_versions(project_id: str, user_id: str = Depends(require_user)) -> dict:
    with closing(connect()) as conn:
        _project_or_404(conn, project_id, user_id)
        rows = conn.execute(
            "SELECT * FROM versions WHERE project_id=? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return {"count": len(rows), "results": [_version_dict(r) for r in rows]}


# ---- 2/3. 引用清单（收藏 + 格式化输出） ----


@router.post("/projects/{project_id}/citations", status_code=201)
def add_citation(
    project_id: str, body: CitationIn, user_id: str = Depends(require_user)
) -> dict:
    """收藏检索命中为引用条目。同 (kind, ref_id) 幂等：重复收藏返回已有条目。"""
    with closing(connect()) as conn:
        _project_or_404(conn, project_id, user_id)
        cid = new_id()
        cur = conn.execute(
            "INSERT OR IGNORE INTO citations (id, project_id, kind, ref_id, meta, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                cid,
                project_id,
                body.kind,
                body.ref_id,
                json.dumps(body.meta, ensure_ascii=False),
                now(),
            ),
        )
        created = cur.rowcount == 1
        conn.commit()
        row = conn.execute(
            "SELECT * FROM citations WHERE project_id=? AND kind=? AND ref_id=?",
            (project_id, body.kind, body.ref_id),
        ).fetchone()
        out = _citation_dict(row)
        out["created"] = created
        return out


@router.get("/projects/{project_id}/citations")
def list_citations(
    project_id: str,
    format: str | None = Query(default=None),
    user_id: str = Depends(require_user),
) -> dict:
    with closing(connect()) as conn:
        _project_or_404(conn, project_id, user_id)
        rows = _list_citations(conn, project_id)
        if format is None:
            return {"count": len(rows), "results": rows}
        fmt = format.strip().lower()
        if fmt not in FORMATTERS:
            raise HTTPException(
                400, f"不支持的 format：{format}（可选 gbt7714 / apa）"
            )
        formatter = FORMATTERS[fmt]
        items = []
        for r in rows:
            text, degraded = formatter(r["meta"])
            items.append(
                {
                    "citation_id": r["id"],
                    "kind": r["kind"],
                    "ref_id": r["ref_id"],
                    "text": text,
                    "degraded": degraded,
                }
            )
        return {"format": fmt, "count": len(items), "items": items}


# ---- 4. 提纲生成（锚定引用） ----


@router.post("/projects/{project_id}/outline")
def generate_outline(
    project_id: str, body: OutlineIn | None = None, user_id: str = Depends(require_user)
) -> dict:
    use_llm = body.use_llm if body else True
    with closing(connect()) as conn:
        project = _project_or_404(conn, project_id, user_id)
        rows = _list_citations(conn, project_id)

    sections = _deterministic_outline(rows, project["kind"])
    engine = "deterministic"
    if not rows:
        llm_info = {"used": False, "note": "无收藏引用，仅结构性骨架"}
    elif use_llm:
        enhanced, note = _llm_enhance(sections, rows, project["kind"])
        llm_info = {"used": enhanced is not None, "note": note}
        if enhanced is not None:
            sections = enhanced
            engine = "llm"
    else:
        llm_info = {"used": False, "note": "未请求 LLM 增强"}

    return {
        "project_id": project_id,
        "engine": engine,
        "llm": llm_info,
        "citation_count": len(rows),
        "section_count": len(sections),
        "sections": sections,
        "generated_at": now(),
    }


# ---- 5. 引用可解校验（D3：来源 ID 注入 + 输出校验 + 渲染层剥除） ----


@router.post("/projects/{project_id}/validate")
def validate_project(
    project_id: str, body: ValidateIn | None = None, user_id: str = Depends(require_user)
) -> dict:
    """三级核验稿件引用：可解 → 真实 → 支持，并按模式处置文本。

    off/shadow 的 cleaned_text 与历史行为逐字节一致（只剥不可解标记）；
    enforce 额外剥除被 block 的引用；pending_review 永不改文本，只在
    annotations 中列出待核原因。
    """
    with closing(connect()) as conn:
        _project_or_404(conn, project_id, user_id)
        citations = _list_citations(conn, project_id)
    valid_ids = {c["id"] for c in citations}

    source, version_id, text = "body", None, (body.text if body else None)
    if text is None or not text.strip():
        with closing(connect()) as conn:
            vrow = conn.execute(
                "SELECT * FROM versions WHERE project_id=? ORDER BY created_at DESC, id"
                " DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if vrow is not None:
            snapshot = json.loads(vrow["snapshot"])
            text = _snapshot_text(snapshot)
            source, version_id = "latest_version", vrow["id"]
    if text is None or not text.strip():
        raise HTTPException(400, "无可校验文本：请传 text 或先保存含 text 的版本快照")

    out = resolve_citations(text, valid_ids)
    mode = verification_mode()
    outcome = verify_text(text, citations, mode=mode, resolver=metadata_resolver)
    return {
        "project_id": project_id,
        "source": source,
        "version_id": version_id,
        "citation_ids_in_collection": sorted(valid_ids),
        **out,
        # 回显被核验的原文：报告里的 locator 字符区间索引的就是这篇文本，
        # 前端据此高亮。enforce 下 cleaned_text 已删标记，与 locator 不再同构。
        "text": text,
        # 文本处置以三级核验结果为准：off/shadow 与上面的 legacy 输出相同，
        # enforce 才会多剥 block 标记。
        "cleaned_text": outcome.cleaned_text,
        "verification": outcome.report.model_dump(mode="json"),
        "verification_mode": mode,
        "annotations": [a.model_dump(mode="json") for a in outcome.annotations],
    }

