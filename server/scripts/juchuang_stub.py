"""剧创线联调桩（task 5.2）：模拟剧创消费方完成一次端到端取卡。

用法（服务侧需先起）：
  FD_SHILUN_AUTH_DEV_TOKENS="juchuang-tok=dramatist" uvicorn fd__api.main:app --port 8010
  python3 scripts/juchuang_stub.py --base http://127.0.0.1:8010 --token juchuang-tok

注意：令牌代表的是「授权了剧创的用户」，不是剧创本身——授权范围在
grants 表（用户→consumer 的开放关系），本桩按此模型调用。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8010"


def call(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--token", required=True)
    args = ap.parse_args()
    BASE = args.base

    # 1) 无授权先试：预期 403（授权边界继承自实论 spec）
    code, body = call("GET", "/api/v1/contract/cards?consumer=juchuang", args.token)
    print(f"[1] 未授权取卡: {code} {body.get('detail', '')[:40]}")
    assert code == 403

    # 2) 用户授权（stance）
    code, body = call("POST", "/api/v1/grants", args.token,
                      {"consumer": "juchuang", "card_kinds": ["stance"]})
    print(f"[2] 授权: {code} {body}")
    assert code == 201

    # 3) 取卡：契约字段完整性
    code, body = call("GET", "/api/v1/contract/cards?consumer=juchuang", args.token)
    print(f"[3] 取卡: {code} contract_version={body.get('contract_version')} "
          f"cards={len(body.get('cards', []))}")
    assert code == 200 and body["contract_version"] == "1"

    if body["cards"]:
        c = body["cards"][0]
        print(f"    首卡: {c['question']} -> {c['position']} (stability={c['stability']})")
        assert all(k in c for k in ("id", "question", "position", "as_of"))

    print("端到端取卡成功（授权→403 边界→授权→契约取卡）；调用留痕在服务端 audit_log。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
