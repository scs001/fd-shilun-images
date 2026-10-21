"""实论 E2E 验收剧本（task 5.1）：双账号全链。

链路：检索收藏引用 → 双格式引用清单 → 提纲锚定 → 引用可解校验；
公文线：文种→模板→骨架稿（样例可溯）。双账号隔离互查。

用法：
  FD_SHILUN_DB=/tmp/shilun_e2e.db FD_SHILUN_AUTH_DEV_TOKENS="tok-a=alice,tok-b=bob" \
  python3 -m uvicorn fd_shilun_api.main:app --port 8015 &
  python3 scripts/e2e_walkthrough.py --base http://127.0.0.1:8015
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

QUERIES = ["extended mind", "free will compatibilism", "moral realism", "physicalism consciousness", "trolley problem ethics"]


def call(base: str, token: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def run_account(base: str, token: str, who: str) -> str:
    code, proj = call(base, token, "POST", "/api/v1/writing/projects",
                      {"title": f"{who} 的论文", "kind": "thesis"})
    assert code in (200, 201), f"{who} 建项目 {code}: {proj}"
    pid = str(proj.get("id") or proj.get("project", {}).get("id"))

    for q in QUERIES:
        code, out = call(base, token, "POST", "/api/v1/search/works", {"query": q})
        assert code == 200, f"{who} 检索 {code}: {out}"
        rs = out.get("results", [])
        assert rs, f"{who} 检索无命中且无原因: {out}"
        top = rs[0]
        ref = top.get("work_id") or top.get("id")
        meta = top.get("meta") or top
        code, _ = call(base, token, "POST", f"/api/v1/writing/projects/{pid}/citations",
                       {"kind": "openalex", "ref_id": str(ref), "meta": {
                           "title": meta.get("title"), "year": meta.get("year"),
                           "venue": meta.get("venue"), "doi": meta.get("doi"),
                           "authors": (meta.get("authors") or [])[:3],
                           "type": "article",
                       }})
        assert code in (200, 201), f"{who} 收藏 {code}"
    print(f"[{who}] 检索+收藏 5 条")

    for fmt in ("gbt7714", "apa"):
        code, out = call(base, token, "GET",
                         f"/api/v1/writing/projects/{pid}/citations?format={fmt}")
        assert code == 200 and len(json.dumps(out)) > 100, f"{who} {fmt} {code}"
    print(f"[{who}] GB/T + APA 双格式 ok")

    code, out = call(base, token, "POST", f"/api/v1/writing/projects/{pid}/outline")
    assert code == 200, f"{who} 提纲 {code}: {out}"
    secs = out.get("sections") or out.get("outline") or []
    assert len(secs) >= 3, f"{who} 提纲节太少: {secs}"
    print(f"[{who}] 提纲 {len(secs)} 节（engine={out.get('engine')}）")

    code, cited = call(base, token, "GET", f"/api/v1/writing/projects/{pid}/citations")
    items = cited if isinstance(cited, list) else cited.get("citations") or cited.get("results") or []
    ids = [str(i.get("ref_id")) for i in items][:2]
    text = "论述一 [[cite:%s]] 论述二 [[cite:%s]] 假引用 [[cite:deadbeef]]" % (ids[0], ids[1])
    code, v = call(base, token, "POST", f"/api/v1/writing/projects/{pid}/validate", {"text": text})
    assert code == 200, f"{who} 校验 {code}: {v}"
    assert v.get("stripped") and "deadbeef" in str(v.get("stripped")), f"{who} 假引用未剥除: {v}"
    assert "[[cite:deadbeef]]" not in (v.get("cleaned_text") or ""), f"{who} 清洗后仍含假引用"
    print(f"[{who}] 引用可解校验 ok（真 {len(v.get('valid', []))} / 假 1 剥除）")
    return pid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8015")
    args = ap.parse_args()

    a = run_account(args.base, "tok-a", "alice")
    b = run_account(args.base, "tok-b", "bob")

    code, out = call(args.base, "tok-b", "GET", "/api/v1/writing/projects")
    lst = out if isinstance(out, list) else out.get("projects") or out.get("results") or []
    assert all(str(p.get("id")) != a for p in lst), "隔离失败"
    print("[隔离] bob 看不到 alice 的项目")

    code, dts = call(args.base, "tok-a", "GET", "/api/v1/gov/doctypes")
    assert code == 200 and dts, f"doctypes {code}"
    dt0 = (dts if isinstance(dts, list) else dts.get("doctypes") or [])[0]
    code, tpl = call(args.base, "tok-a", "GET", f"/api/v1/gov/template?doctype={urllib.parse.quote(dt0.get('doctype', ''))}")
    assert code == 200 and (tpl.get("samples") or tpl.get("sample_ids")), f"模板无样例 {tpl}"
    code, draft = call(args.base, "tok-a", "POST", "/api/v1/gov/draft",
                       {"doctype": dt0.get("doctype"), "subject": "开展2026年度监督检查",
                        "key_points": ["明确范围", "落实整改"]})
    assert code == 200, f"草稿 {code}: {draft}"
    body = json.dumps(draft, ensure_ascii=False)
    assert ("sample" in body.lower()) or ("doc_" in body), "草稿无样例可溯标注"
    print(f"[公文] {dt0.get('doctype')} 模板+骨架稿 ok（样例可溯）")

    print("\n实论 E2E 双账号全链通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
