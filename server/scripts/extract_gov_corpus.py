#!/usr/bin/env python3
"""导出 gov 垂域语料 -> data/gov_corpus.json（shilun-serious-writing task 3.1 前置）。

语料真源是 fd-text-core 的 PG（corpus_documents.domain='gov'，status='current'）。
本地未落该库，从生产只读取数（DDL/DML 零执行，连接只发 SELECT）：

    # 取 DSN（只读 env）：
    kubectl --context cheap -n fd-prod exec deploy/fd-text-core -- \\
        env | grep -i fdtext_database_url
    # 直连导出（100.64.0.10 为集群内 PG，网内可达，实测直连成功）：
    FDTEXT_DATABASE_URL='postgresql://...' python3 scripts/extract_gov_corpus.py

字段口径参照归档报告
openspec/changes/archive/2026-10-13-gov-documents-vertical/reports/acceptance.md §二
（title/doc_number/issuer/date/doc_type/sections/cited_docs 的实测语义）：
- sections 取自 metadata.sections（解析器的层级切分，{path,text} 形态），
  不取 content 列（那里被 loader 拼了检索头 index_header，非原文）。
- date 取 doc_date（落款优先的成文日期），缺失回落 published。
- doc_type 保留原库标注（可为 null，23 篇标题无法判定）；下游 gov.py 另做
  规则分类，不信任此字段也不改写它。
- doc_id 形如 "gov:mnr:<source_id>"（source_id 即规范化 URL，可点开验证），
  另附 source_url 便于前端直接跳转。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "data" / "gov_corpus.json"


def build_docs(rows: list[dict]) -> list[dict]:
    docs: list[dict] = []
    for row in rows:
        md = row.get("metadata") or {}
        sections = [
            {"section_path": str(s.get("path") or ""), "text": str(s.get("text") or "")}
            for s in (md.get("sections") or [])
            if str(s.get("text") or "").strip()
        ]
        doc_type = md.get("doc_type")
        docs.append(
            {
                "doc_id": f"gov:{row['source']}:{row['source_id']}",
                "source": row["source"],
                "source_url": str(md.get("source_url") or row["source_id"]),
                "title": str(row.get("title") or "").strip(),
                "doc_number": md.get("doc_number"),
                "issuer": md.get("issuer"),
                "issuer_raw": md.get("issuer_raw"),
                "doc_type": str(doc_type).strip() if doc_type else None,
                "date": md.get("doc_date") or md.get("published"),
                "addressee": md.get("addressee") or None,
                "sections": sections,
                "cited_docs": [str(c) for c in (md.get("cited_docs") or [])],
            }
        )
    return docs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--database-url",
        default=None,
        help="fd-text-core PG DSN；缺省读 FDTEXT_DATABASE_URL（生产取法见模块 docstring）",
    )
    ap.add_argument("--domain", default="gov")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    import os

    dsn = args.database_url or os.environ.get("FDTEXT_DATABASE_URL", "").strip()
    if not dsn:
        print("缺少数据库 DSN：传 --database-url 或设 FDTEXT_DATABASE_URL", file=sys.stderr)
        return 2

    import psycopg

    sql = (
        "SELECT source, source_id, title, metadata "
        "FROM corpus_documents "
        "WHERE domain = %s AND status = 'current' "
        "ORDER BY source, source_id"
    )
    with psycopg.connect(dsn) as conn:  # 只读：不 commit，退出即丢弃事务
        with conn.cursor() as cur:
            cur.execute(sql, (args.domain,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    docs = build_docs(rows)
    if not docs:
        print("导出 0 篇——检查 domain/status 过滤条件", file=sys.stderr)
        return 1
    empty_title = sum(1 for d in docs if not d["title"])
    empty_sections = sum(1 for d in docs if not d["sections"])

    payload = {
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "kind": "fd-text-core-pg",
            "table": "corpus_documents",
            "domain": args.domain,
            "status": "current",
            "read_only": True,
        },
        "doc_count": len(docs),
        "docs": docs,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    by_source: dict[str, int] = {}
    for d in docs:
        by_source[d["source"]] = by_source.get(d["source"], 0) + 1
    print(f"[extract] {len(docs)} docs -> {args.out}")
    print(f"[extract] by source: {dict(sorted(by_source.items()))}")
    print(f"[extract] 无标题 {empty_title} 篇 / 无正文层级 {empty_sections} 篇（如实计数）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
