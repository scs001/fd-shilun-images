"""公文模板归纳与写作流（shilun-serious-writing task 3.1/3.2/3.3）。

数据面：data/gov_corpus.json（scripts/extract_gov_corpus.py 从 fd-text-core PG
只读导出的 gov 垂域语料，212 篇）。本模块**只读**该文件，不连库。

三件事：
1. 文种分类（rule-v1）：标题文种词（关于……的通知/请示/函/意见/办法……）优先，
   部令编号形态（X令第N号 → 命令）与「公示」包含式兜底；不确定标 unknown。
   与原库 parser 标注独立，互不改写，/doctypes 同时报告两者一致率（抽检代理）。
2. 结构模板归纳（task 3.2）：每文种从**真实样例**归纳节结构（标题/文号/主送/
   正文层级/结语/落款）与规范约束（称谓/层级用语/格式要素），附源样例 doc_id
   （≥3 可溯；不足 3 篇标 sample_insufficient，规范降为通则参考）。
3. 写作流（task 3.3）：POST /draft 确定性拼装骨架稿（占位+规范提示），
   可选 LLM 润色（fd_shilun_api.llm，无 KEY/失败即降级，铁律不自研重试），
   响应携带所用模板版本与样例引用（可点开验证：GET /samples/{doc_id}）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import Counter
from datetime import date as _date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_user
from . import llm

router = APIRouter(prefix="/api/v1/gov", tags=["gov"])

RULE_VERSION = "rule-v1"
TEMPLATE_VERSION = "gov-tpl-v1"
MIN_SAMPLES = 3  # spec：模板须可溯源到 ≥3 条真实样例；不足即标 sample_insufficient

# ---------------------------------------------------------------------------
# 文种分类（rule-v1）
# ---------------------------------------------------------------------------

# 标题尾词表：《党政机关公文处理工作条例》15 类 + 实测出现的公示/规章类尾词。
# 匹配取最长优先（「管理办法」先于「办法」）。
_TAIL_WORDS: tuple[str, ...] = tuple(
    sorted(
        [
            "请示", "批复", "通知", "通报", "通报表扬", "公报", "公告", "通告",
            "意见", "决定", "决议", "报告", "议案", "函", "复函", "纪要", "公示",
            "管理办法", "暂行办法", "实施办法", "办法", "规则", "条例", "规定",
            "细则", "方案",
        ],
        key=len,
        reverse=True,
    )
)
# 尾词到文种的归一（复函→函；规章尾词保留原词作为文种——部令形态会先被命令规则接住）
_TAIL_MAP = {"复函": "函"}

# 已知文种全集：分类产出 + 条例法定文种（0 样例文种也可出模板/草稿，标样例不足）
KNOWN_DOCTYPES: frozenset[str] = frozenset(
    {
        "决议", "决定", "命令", "公报", "公告", "通告", "意见", "通知", "通报",
        "报告", "请示", "批复", "议案", "函", "纪要",
        # 实测语料出现的事务/规章文类
        "公示", "办法", "规则", "条例", "规定", "细则", "方案",
    }
)

_RE_LING = re.compile(r"令第\d+号")  # 部令编号形态（标题或文号里出现即命令）
_RE_TAIL_PAREN = re.compile(r"（[^（）]*）\s*$")  # 尾部批次括注：公示（一）→ 公示


def _normalize_title_tail(title: str) -> str:
    """剥掉标题尾部的批次括注（「拟聘人员公示（一）」→「…公示」），只用于文种判定。"""
    t = (title or "").strip()
    while True:
        t2 = _RE_TAIL_PAREN.sub("", t).strip()
        if t2 == t:
            return t
        t = t2


def classify_doc_type(title: str | None, doc_number: str | None = None) -> str:
    """规则文种标注。优先级：部令形态 > 标题尾词 > 「公示」包含式 > unknown。

    不确定就标 unknown——不拿 issuer 硬猜（issuer 只影响机关面，不影响文种）。
    """
    title = (title or "").strip()
    hay = f"{title}{doc_number or ''}"
    if _RE_LING.search(hay):
        return "命令"
    tail = _normalize_title_tail(title)
    for word in _TAIL_WORDS:
        if tail.endswith(word):
            return _TAIL_MAP.get(word, word)
    if "公示" in title:
        return "公示"
    return "unknown"


# ---------------------------------------------------------------------------
# 语料加载（只读 data/gov_corpus.json；缺失即诚实 503，不臆造空语料）
# ---------------------------------------------------------------------------

class CorpusUnavailable(RuntimeError):
    """gov 语料未就位（未导出/路径错/坏 JSON）。"""


_corpus_cache: list[dict] | None = None
_corpus_meta: dict[str, Any] = {}
_corpus_lock = threading.Lock()
_stats_cache: dict[str, Any] | None = None


def _corpus_path() -> Path:
    env = os.environ.get("FD_SHILUN_GOV_CORPUS_PATH", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "gov_corpus.json"


def load_corpus() -> list[dict]:
    """读入并缓存语料文档列表。坏文件抛 CorpusUnavailable（端点转 503）。"""
    global _corpus_cache, _corpus_meta
    if _corpus_cache is not None:
        return _corpus_cache
    with _corpus_lock:
        if _corpus_cache is not None:
            return _corpus_cache
        path = _corpus_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            docs = payload["docs"]
        except Exception as exc:  # noqa: BLE001 —— 任何读取失败都归一为语料不可用
            raise CorpusUnavailable(
                f"gov 语料不可用（{path}）: {type(exc).__name__}: {exc}"
            ) from exc
        _corpus_meta = {
            "extracted_at": payload.get("extracted_at"),
            "doc_count": payload.get("doc_count", len(docs)),
            "source": payload.get("source"),
        }
        _corpus_cache = list(docs)
        return _corpus_cache


def _stats() -> dict[str, Any]:
    """全量分类统计（内置统计表）。首算后缓存。"""
    global _stats_cache
    if _stats_cache is not None:
        return _stats_cache
    docs = load_corpus()
    by_type: dict[str, list[dict]] = {}
    agree = disagree = unlabeled = 0
    for d in docs:
        label = classify_doc_type(d.get("title"), d.get("doc_number"))
        by_type.setdefault(label, []).append(d)
        src = d.get("doc_type")
        if not src:
            unlabeled += 1
        elif src == label:
            agree += 1
        else:
            disagree += 1
    _stats_cache = {
        "rule_version": RULE_VERSION,
        "total": len(docs),
        "by_type": by_type,
        "agreement_vs_source_label": {
            "labeled": agree + disagree,
            "agree": agree,
            "disagree": disagree,
            "unlabeled_in_source": unlabeled,
            "rate": round(agree / (agree + disagree), 4) if agree + disagree else None,
        },
        "meta": dict(_corpus_meta),
    }
    return _stats_cache


def _representative(docs: list[dict], n: int) -> list[dict]:
    """代表样例：层级最丰富（sections 最多）优先，doc_id 决定性排序。"""
    ranked = sorted(docs, key=lambda d: (-len(d.get("sections") or []), d["doc_id"]))
    return ranked[:n]


# ---------------------------------------------------------------------------
# 结构归纳（task 3.2）
# ---------------------------------------------------------------------------

_RE_CN_DATE = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日")
_RE_ORG_SUFFIX = re.compile(r"[厅局委部院司府所团心]")

_CLOSING_PHRASES = [
    "特此通知", "特此函告", "特此函复", "特此公告", "现予公告", "现予公布",
    "特此通报", "特此报告", "妥否，请批示", "当否，请批示", "请予批复",
    "此复", "特此函复", "请认真贯彻执行", "请遵照执行",
]

# 层级标记匹配对象是逐行拼接的 section_path（每行一个路径，嵌套用「 > 」连接），
# ^ 必须 re.M 按行锚定——否则只匹配首行，顶层「一、」全漏（实测踩坑）。
_HIERARCHY_PATTERNS = {
    "一、": re.compile(r"^[一二三四五六七八九十百]+、| > [一二三四五六七八九十百]+、", re.M),
    "（一）": re.compile(r"^（[一二三四五六七八九十百]+）| > （[一二三四五六七八九十百]+）", re.M),
    "1.": re.compile(r"^\d{1,3}[.．]| > \d{1,3}[.．]", re.M),
    "第X条": re.compile(r"^第[一二三四五六七八九十百]+条", re.M),
}


def _doc_addressee_line(doc: dict) -> str | None:
    """主送机关行：前言节里标题之后、以「：」结尾的短机构行。"""
    secs = doc.get("sections") or []
    if not secs:
        return None
    lines = [ln.strip() for ln in secs[0].get("text", "").split("\n") if ln.strip()]
    title = (doc.get("title") or "").strip()
    idx = 1 if lines and lines[0] == title else 0
    for ln in lines[idx : idx + 3]:
        if ln.endswith("：") and 4 <= len(ln) <= 100 and _RE_ORG_SUFFIX.search(ln[:-1]):
            return ln[:-1]
    return None


def _doc_uses_hierarchy(doc: dict) -> dict[str, bool]:
    paths = [s.get("section_path") or "" for s in doc.get("sections") or []]
    joined = "\n".join(paths)
    return {name: bool(p.search(joined)) for name, p in _HIERARCHY_PATTERNS.items()}


def _doc_closing(doc: dict) -> str | None:
    tail_text = "\n".join(s.get("text", "") for s in (doc.get("sections") or [])[-2:])
    for phrase in _CLOSING_PHRASES:
        if phrase in tail_text:
            return phrase
    return None


def _doc_has_signature_block(doc: dict) -> dict[str, bool]:
    tail_text = "\n".join(s.get("text", "") for s in (doc.get("sections") or [])[-2:])
    issuer = doc.get("issuer_raw") or doc.get("issuer") or ""
    return {
        "issuer": bool(issuer) and issuer in tail_text,
        "date": bool(_RE_CN_DATE.search(tail_text)),
    }


def _title_pattern_rate(docs: list[dict], doctype: str) -> float:
    pat = re.compile(r"关于.+的")
    hit = sum(1 for d in docs if pat.search(_normalize_title_tail(d.get("title") or "")))
    return round(hit / len(docs), 4) if docs else 0.0


# 样例不足文种的通则骨架（明确标注非语料归纳，来源《党政机关公文处理工作条例》通行体式）
_GENERIC_CONSTRAINTS_NOTE = "样例不足，仅结构参考：约束为公文通行体式（非本库语料归纳）"

_GENERIC_CLOSINGS = {
    "请示": "妥否，请批示。",
    "批复": "此复。",
    "报告": "特此报告。",
    "函": "特此函告。",
    "通知": "特此通知。",
    "通报": "特此通报。",
    "公告": "现予公告。",
    "命令": "现予公布。",
    "意见": "请认真贯彻执行。",
    "决定": "特此决定。",
}

_GENERIC_OPENERS = {
    "通知": "为了{purpose}，根据{basis}，现将有关事项通知如下：",
    "请示": "为了{purpose}，根据{basis}，现将有关事项请示如下：",
    "函": "为了{purpose}，根据{basis}，现将有关事项函告如下：",
    "报告": "为了{purpose}，根据{basis}，现将有关情况报告如下：",
    "批复": "你单位《关于{subject}的请示》收悉。经研究，现批复如下：",
    "意见": "为了{purpose}，根据{basis}，制定本意见。",
    "公告": "根据{basis}，决定{subject}。现予公告：",
    "通报": "为了{purpose}，现将有关情况通报如下：",
    "决定": "为了{purpose}，根据{basis}，决定如下：",
    "命令": "为了{purpose}，根据{basis}，现公布如下：",
}


def build_template(doctype: str) -> dict[str, Any]:
    """从语料归纳某文种的结构模板 + 规范约束 + 源样例。"""
    stats = _stats()
    docs = stats["by_type"].get(doctype, [])
    n = len(docs)
    insufficient = n < MIN_SAMPLES
    samples = _representative(docs, 5)

    if docs:
        addr_rate = round(
            sum(1 for d in docs if _doc_addressee_line(d)) / n, 4
        )
        hier_counts = Counter()
        for d in docs:
            for name, used in _doc_uses_hierarchy(d).items():
                if used:
                    hier_counts[name] += 1
        closing_counts = Counter(c for c in (_doc_closing(d) for d in docs) if c)
        sig = Counter()
        for d in docs:
            for k, v in _doc_has_signature_block(d).items():
                if v:
                    sig[k] += 1
        docno_rate = round(sum(1 for d in docs if d.get("doc_number")) / n, 4)
        cited_rate = round(sum(1 for d in docs if d.get("cited_docs")) / n, 4)
        title_rate = _title_pattern_rate(docs, doctype)
        top_closings = [
            {"phrase": p, "count": c, "rate": round(c / n, 4)}
            for p, c in closing_counts.most_common(5)
        ]
        addr_forms = Counter(
            a for a in (_doc_addressee_line(d) for d in docs) if a
        ).most_common(3)
    else:  # 0 样例：证据面全部置空，走通则
        addr_rate = hier_counts = closing_counts = sig = None  # type: ignore[assignment]
        docno_rate = cited_rate = title_rate = 0.0
        top_closings = []
        addr_forms = []

    observed_closing = top_closings[0]["phrase"] if top_closings else None
    closing_spec = observed_closing or _GENERIC_CLOSINGS.get(doctype, "特此。")

    def _rate(num: int | None) -> float | None:
        return round(num / n, 4) if (num is not None and n) else None

    structure = [
        {
            "element": "标题",
            "spec": "发文机关+关于+事由+的{doctype}（联合行文机关并列；命令类为「机关令第N号」）",
            "placeholder": "〔发文机关〕关于〔事由〕的" + doctype,
            "evidence": {"about_pattern_rate": title_rate},
        },
        {
            "element": "文号",
            "spec": "机关代字〔年份〕序号号（六角括号〔〕；命令类为「机关令第N号」）",
            "placeholder": "〔机关代字〔20XX〕X号〕",
            "evidence": {"doc_number_rate": docno_rate},
        },
        {
            "element": "主送机关",
            "spec": "主送机关全称+「：」；公告/命令类按体例可无主送",
            "placeholder": "〔主送机关全称〕：",
            "evidence": {"addressee_rate": addr_rate},
        },
        {
            "element": "正文·前言",
            "spec": "缘由（目的+依据）引出事项，「现将有关事项{doctype}如下：」式过渡语",
            "placeholder": _GENERIC_OPENERS.get(doctype, "为了{purpose}，根据{basis}，现将有关事项如下："),
            "evidence": None if insufficient else {"hierarchy_docs": n},
        },
        {
            "element": "正文·主体层级",
            "spec": "一级「一、」→ 二级「（一）」→ 三级「1.」；规章/命令类用「第X条」",
            "placeholder": "一、〔要点一〕\n（一）〔细化〕\n1.〔再细化〕",
            "evidence": None
            if insufficient
            else {k: _rate(v) for k, v in hier_counts.items()},
        },
        {
            "element": "结语用语",
            "spec": "文种收束套语（语料实证见 observed）",
            "placeholder": closing_spec,
            "evidence": None if insufficient else {"observed": top_closings},
        },
        {
            "element": "落款",
            "spec": "发文机关署名 + 成文日期（YYYY年M月D日，位于正文末）",
            "placeholder": "〔发文机关〕\n20XX年X月X日",
            "evidence": None
            if insufficient
            else {"issuer_rate": _rate(sig.get("issuer", 0)), "date_rate": _rate(sig.get("date", 0))},
        },
    ]

    constraints = {
        "称谓": {
            "observed_forms": [
                {"text": t, "count": c} for t, c in addr_forms
            ],
            "note": "主送机关用全称或规范化简称，多主送按隶属关系排序，以「：」收束"
            + ("；本库样例中主送出现率见 evidence" if not insufficient else ""),
        },
        "层级用语": {
            "observed": None
            if insufficient
            else {k: _rate(v) for k, v in hier_counts.items()},
            "note": "一级用「一、」、二级用「（一）」、三级用「1.」；条规类文种用「第X条」",
        },
        "格式要素": {
            "doc_number_rate": docno_rate,
            "cited_doc_number_rate": cited_rate,
            "date_format": "YYYY年M月D日",
            "note": "结构层次序数、日期用字与六角括号文号为公文格式要件（GB/T 9704 体系）",
        },
    }

    digest = hashlib.md5(
        f"{doctype}|{','.join(d['doc_id'] for d in samples)}|{_corpus_meta.get('extracted_at')}".encode()
    ).hexdigest()[:8]

    return {
        "doctype": doctype,
        "version": f"{TEMPLATE_VERSION}:{digest}",
        "rule_version": RULE_VERSION,
        "sample_count": n,
        "sample_insufficient": insufficient,
        "note": _GENERIC_CONSTRAINTS_NOTE if insufficient else None,
        "structure": structure,
        "constraints": constraints,
        "sample_doc_ids": [d["doc_id"] for d in samples],
        "samples": [
            {
                "doc_id": d["doc_id"],
                "title": d.get("title"),
                "issuer": d.get("issuer"),
                "date": d.get("date"),
                "source_url": d.get("source_url"),
            }
            for d in samples
        ],
    }


# ---------------------------------------------------------------------------
# 骨架稿拼装（task 3.3，确定性层）
# ---------------------------------------------------------------------------

_CN_NUM = "一二三四五六七八九十"


def _cn_index(i: int) -> str:
    return _CN_NUM[i] if i < 10 else str(i + 1)


def assemble_draft(
    doctype: str,
    subject: str,
    key_points: list[str],
    issuer: str,
    addressee: str | None,
) -> list[dict[str, str]]:
    """确定性模板拼装：每节 {element, text, norm}。占位符以〔〕标示。"""
    tpl = build_template(doctype)
    today = _date.today()
    cn_date = f"{today.year}年{today.month}月{today.day}日"

    title = f"{issuer}关于{subject}的{doctype}" if issuer else f"关于{subject}的{doctype}"
    opener = _GENERIC_OPENERS.get(doctype, "为了{purpose}，根据{basis}，现将有关事项如下：")
    opener_text = (
        opener.replace("{purpose}", f"〔目的：{subject}〕")
        .replace("{basis}", "〔依据：相关法规/上位文件〕")
        .replace("{subject}", subject)
    )

    if key_points:
        body_lines = [
            f"{_cn_index(i)}、{kp}" for i, kp in enumerate(key_points)
        ]
        body_text = "\n".join(body_lines)
    else:
        body_text = "一、〔要点一：具体事项/安排〕\n二、〔要点二：职责分工/时限要求〕"

    sections_out: list[dict[str, str]] = [
        {
            "element": "标题",
            "text": title,
            "norm": "发文机关+关于+事由+文种；事由前「关于」、后「的」，不加标点",
        },
        {
            "element": "文号",
            "text": "〔机关代字〔%d〕XX号〕" % today.year,
            "norm": "机关代字+六角括号年份+序号；联合行文只标注主办机关文号",
        },
        {
            "element": "主送机关",
            "text": (addressee.rstrip("：:") + "：") if addressee else "〔主送机关全称〕：",
            "norm": "公告/命令类按体例可无主送；多主送按隶属关系排序",
        },
        {
            "element": "正文·前言",
            "text": opener_text,
            "norm": "缘由=目的+依据，过渡到「现将…如下：」式引导语",
        },
        {
            "element": "正文·主体",
            "text": body_text,
            "norm": "一级「一、」二级「（一）」三级「1.」；条规类用「第X条」",
        },
        {
            "element": "结语",
            "text": tpl["structure"][5]["placeholder"],
            "norm": "文种收束套语，独立成段",
        },
        {
            "element": "落款",
            "text": f"{issuer or '〔发文机关〕'}\n{cn_date}",
            "norm": "发文机关署名+成文日期（YYYY年M月D日），位于末尾",
        },
    ]
    return sections_out


# ---------------------------------------------------------------------------
# API 模型与端点
# ---------------------------------------------------------------------------

class DraftRequest(BaseModel):
    doctype: str = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=2, max_length=120)
    key_points: list[str] = Field(default_factory=list, max_length=12)
    issuer: str = Field(default="〔发文机关〕", max_length=60)
    addressee: str | None = Field(default=None, max_length=120)
    polish: bool = True  # 可选 LLM 润色；无 KEY/失败自动降级为骨架稿


def _corpus_or_503() -> list[dict]:
    try:
        return load_corpus()
    except CorpusUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/doctypes")
def list_doctypes(user_id: str = Depends(require_user)) -> dict:
    """文种列表 + 样例数 + 代表样例 doc_id（内置统计表）。"""
    _corpus_or_503()
    stats = _stats()
    doctypes = []
    for name in sorted(stats["by_type"], key=lambda k: (-len(stats["by_type"][k]), k)):
        docs = stats["by_type"][name]
        doctypes.append(
            {
                "doctype": name,
                "count": len(docs),
                "sample_insufficient": len(docs) < MIN_SAMPLES,
                "sample_doc_ids": [d["doc_id"] for d in _representative(docs, 5)],
            }
        )
    return {
        "rule_version": RULE_VERSION,
        "total": stats["total"],
        "doctypes": doctypes,
        "agreement_vs_source_label": stats["agreement_vs_source_label"],
        "corpus": {
            "extracted_at": stats["meta"].get("extracted_at"),
            "doc_count": stats["meta"].get("doc_count"),
        },
    }


@router.get("/template")
def get_template(doctype: str, user_id: str = Depends(require_user)) -> dict:
    """结构模板 + 规范约束 + 源样例（≥3 可溯；不足即标 sample_insufficient）。"""
    _corpus_or_503()
    if doctype not in KNOWN_DOCTYPES:
        raise HTTPException(
            status_code=422,
            detail=f"未知文种 {doctype!r}；可用：{', '.join(sorted(KNOWN_DOCTYPES))}",
        )
    return build_template(doctype)


@router.get("/samples/{doc_id:path}")
def get_sample(doc_id: str, user_id: str = Depends(require_user)) -> dict:
    """样例条目详情：引用可点开验证的落点。"""
    docs = _corpus_or_503()
    target = doc_id.strip().rstrip("/")
    for d in docs:
        if d["doc_id"] == target:
            return {
                "doc_id": d["doc_id"],
                "title": d.get("title"),
                "issuer": d.get("issuer"),
                "issuer_raw": d.get("issuer_raw"),
                "doc_number": d.get("doc_number"),
                "doc_type_labeled": d.get("doc_type"),
                "doc_type_classified": classify_doc_type(d.get("title"), d.get("doc_number")),
                "date": d.get("date"),
                "source_url": d.get("source_url"),
                "cited_docs": d.get("cited_docs"),
                "sections": [
                    {"section_path": s.get("section_path"), "preview": (s.get("text") or "")[:160]}
                    for s in (d.get("sections") or [])[:12]
                ],
            }
    raise HTTPException(status_code=404, detail=f"语料中无此条目：{doc_id!r}")


@router.post("/draft")
def post_draft(req: DraftRequest, user_id: str = Depends(require_user)) -> dict:
    """模板约束下的骨架稿生成（可选 LLM 润色），引用样例可溯。"""
    _corpus_or_503()
    if req.doctype not in KNOWN_DOCTYPES:
        raise HTTPException(
            status_code=422,
            detail=f"未知文种 {req.doctype!r}；可用：{', '.join(sorted(KNOWN_DOCTYPES))}",
        )
    tpl = build_template(req.doctype)
    sections = assemble_draft(
        req.doctype, req.subject.strip(), [kp.strip() for kp in req.key_points if kp.strip()],
        req.issuer.strip(), req.addressee,
    )

    result: dict[str, Any] = {
        "doctype": req.doctype,
        "title": sections[0]["text"],
        "sections": sections,
        "polished": False,
        "polish_note": None,
        "template": {
            "doctype": req.doctype,
            "version": tpl["version"],
            "sample_count": tpl["sample_count"],
            "sample_insufficient": tpl["sample_insufficient"],
            "sample_doc_ids": tpl["sample_doc_ids"][:5],
        },
        "citations": tpl["samples"][:5],
        "warnings": [],
    }
    if tpl["sample_insufficient"]:
        result["warnings"].append(
            f"样例不足：文种「{req.doctype}」语料样例仅 {tpl['sample_count']} 条（<{MIN_SAMPLES}），"
            "模板与规范为通行体式参考，非本库语料归纳"
        )

    skeleton_text = "\n\n".join(
        f"【{s['element']}】\n{s['text']}" for s in sections
    )
    if req.polish and llm.is_configured():
        sample_excerpts = "\n".join(
            f"- {s['title']}（{s.get('issuer') or ''} {s.get('date') or ''}）："
            for s in tpl["samples"][:3]
        )
        try:
            polished = llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是公文写作助手。严格按给定骨架的分节结构成文，"
                            "遵守规范提示（称谓全称、层级 一、/（一）/1.、结语套语、落款日期格式），"
                            "不虚构事实与文号，占位符〔〕内容按上下文补写或保留。"
                            "只输出成文正文，不要解释。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"文种：{req.doctype}\n事由：{req.subject}\n"
                            f"发文机关：{req.issuer}\n主送：{req.addressee or '〔待定〕'}\n"
                            f"要点：{json.dumps([kp for kp in req.key_points], ensure_ascii=False)}\n\n"
                            f"骨架：\n{skeleton_text}\n\n参考样例（仅体式参考，不得照抄内容）：\n{sample_excerpts}"
                        ),
                    },
                ],
                max_tokens=1400,
            )
        except llm.LLMError as exc:
            result["polish_note"] = f"LLM 润色失败，已降级为骨架稿：{exc}"
        else:
            if polished:
                result["polished"] = True
                result["polished_text"] = polished
    elif req.polish:
        result["polish_note"] = "未配置 FD_SHILUN_LLM_API_KEY，跳过润色（骨架稿为确定性产出）"

    return result
