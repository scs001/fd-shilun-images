#!/usr/bin/env python3
"""中文授权通道契约核查（deploy/COMPLIANCE.md §1 的执行器）。

问题
----
`fd_shilun_api/channels.py` 对厂商 HTTP 响应的解析是**假定**的（顶层形状
results/items/data.records，字段接受中英别名）。上线前必须确认「厂商实际返回」
与「我们的假设」一致，否则用户会看到 not_indexed 或 upstream 这类假失败。

做法
----
拿真实凭据打一次真实请求，逐层比对：
1. 请求是否成功（HTTP 状态、鉴权是否被接受）
2. 响应顶层形状是否被识别
3. 首条记录的每个字段能否解析出来
4. 不匹配处给出「该改哪一处」的提示

退出码 0 = 契约吻合；非 0 = 契约不符或环境未配好。

用法
----
    export FD_SHILUN_CN_CHANNELS='[{"id":"wanfang","kind":"http", ...}]'
    export WANFANG_API_KEY='...'
    python3 scripts/check_cn_channel.py --query 乡村振兴
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fd_shilun_api import channels  # noqa: E402

# 解析器接受的顶层记录容器（与 channels._payload_records 口径一致）
_KNOWN_SHAPES = ("results", "items", "records", "data.records")


def _shape_of(payload: Any) -> str | None:
    """识别响应顶层形状；认不出返回 None。"""
    if not isinstance(payload, dict):
        return None
    for key in ("results", "items", "records"):
        if isinstance(payload.get(key), list):
            return key
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return "data.records"
    return None


def _records_of(payload: Any) -> list[Any]:
    shape = _shape_of(payload)
    if shape is None:
        return []
    if shape == "data.records":
        return list(payload["data"]["records"])
    return list(payload[shape])


def _probe(config: channels.ChannelConfig, query: str) -> int:
    print(f"\n{'=' * 62}")
    print(f"通道：{config.id}（{config.label}）")
    print(f"{'=' * 62}")

    if not config.usable:
        print(f"✗ 配置不可用：{config.problem}")
        print("  该改哪里：按 channels.py 模块 docstring 的字段表补齐该项。")
        return 2
    if config.kind != "http":
        print(f"跳过：kind={config.kind!r}。sandbox 通道不构成对外授权，不能用于契约核查。")
        return 2

    print(f"端点：{config.base_url}")
    print(f"鉴权：{config.auth_scheme}"
          + (f" / 头名 {config.header_name}" if config.auth_scheme == "header" else ""))
    print(f"密钥来源：环境变量 {config.api_key_env}")
    print(f"查询：{query!r}")

    api_key = (os.environ.get(config.api_key_env or "") or "").strip()
    if not api_key:
        print(f"\n✗ 环境变量 {config.api_key_env} 未设置或为空")
        print("  该改哪里：export 该变量为厂商提供的密钥。")
        return 2

    # 1. 直接发请求，拿原始 JSON（绕过解析，便于比对形状）
    raw, failure, detail = _send(config, api_key, query)
    if failure:
        print(f"\n✗ 请求失败：{failure} — {detail}")
        print("  该改哪里：检查 base_url / 鉴权方式 / 头名是否与厂商文档一致。")
        return 1
    print("✓ 请求成功（HTTP 2xx，鉴权被接受）")

    # 2. 顶层形状
    shape = _shape_of(raw)
    if shape is None:
        print("\n✗ 响应顶层形状未被识别")
        print(f"  实际顶层键：{sorted(raw.keys()) if isinstance(raw, dict) else type(raw).__name__}")
        print(f"  我们接受：{', '.join(_KNOWN_SHAPES)}")
        print("  该改哪里：channels._payload_records 增加该形状分支。")
        return 1
    print(f"✓ 顶层形状识别为 {shape!r}")

    # 3. 记录与字段
    records = _records_of(raw)
    if not records or not isinstance(records[0], dict):
        print("\n✗ 响应里没有可用记录（空结果）")
        print("  可能是查询词无命中，也可能是形状识别错。换一个更宽的查询词再试。")
        return 3

    first = records[0]
    print(f"✓ 首条记录可解析，字段：{sorted(first.keys())}")

    print("\n逐字段比对（normalize_record 的实际产出）：")
    normalized = channels.normalize_record(first)
    for key in ("stable_id", "title", "authors", "year", "venue", "doi", "abstract", "url"):
        value = normalized.get(key)
        mark = "✓" if value not in (None, "", []) else "·"
        shown = repr(value)[:70] if value not in (None, "", []) else "（空）"
        print(f"  {mark} {key:<10} {shown}")

    critical = [k for k in ("stable_id", "title") if normalized.get(k) in (None, "")]
    if critical:
        print(f"\n✗ 关键字段解析为空：{', '.join(critical)}")
        print("  该改哪里：channels.normalize_record 给该字段补上厂商用的键名。")
        print(f"  厂商实际字段：{sorted(first.keys())}")
        return 1

    # 4. 端到端：走真实解析路径
    print("\n端到端（走 channels.search_chinese 的真实解析）：")
    channel = channels.channel_from_config(config)
    if channel is None:
        print("✗ channel_from_config 返回 None（配置虽可用但构造失败）")
        return 1
    data = channels.search_chinese(query, page=1, per_page=5, channels=[channel])
    parsed = data.get("records") or []
    if not parsed:
        print(f"✗ 解析后无记录：reason={data.get('reason')} detail={data.get('detail')}")
        return 1
    print(f"✓ 解析出 {len(parsed)} 条；首条：")
    for key in ("stable_id", "title", "year", "venue", "doi"):
        print(f"    {key:<10} {parsed[0].get(key)!r}")
    if not data.get("authorization_basis"):
        print("✗ 授权依据为空：license 字段没配，不算授权通道")
        return 1
    print(f"✓ 授权依据：{data['authorization_basis']}")

    print("\n结论：契约吻合，可以上线。")
    return 0


def _send(
    config: channels.ChannelConfig, api_key: str, query: str
) -> tuple[Any, str | None, str]:
    """直接发一次请求拿原始 JSON（绕过解析，便于比对形状）。"""
    if channels.httpx is None:
        return None, "unreachable", "httpx 未安装（pip install httpx）"
    body = {"query": query, "page": 1, "per_page": 5}
    headers = {"Content-Type": "application/json"}
    if config.auth_scheme == "header":
        headers[config.header_name or "X-API-KEY"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with channels.httpx.Client(timeout=channels._TIMEOUT_SECONDS) as client:
            resp = client.post(config.base_url or "", json=body, headers=headers)
    except Exception as exc:  # noqa: BLE001 —— 核查脚本要把任何失败说清楚
        return None, "unreachable", f"{type(exc).__name__}: {exc}"
    if resp.status_code >= 400:
        return None, "upstream", f"HTTP {resp.status_code}: {resp.text[:300]}"
    try:
        return resp.json(), None, ""
    except ValueError:
        return None, "upstream", f"响应不是 JSON：{resp.text[:300]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="中文授权通道契约核查")
    parser.add_argument("--query", default="乡村振兴", help="探针查询词")
    parser.add_argument("--channel", default=None, help="只核查指定通道 id")
    args = parser.parse_args(argv)

    configs = channels.configured_channels()
    if not configs:
        print("✗ 未配置任何通道（FD_SHILUN_CN_CHANNELS 为空或格式不对）")
        print("  该改哪里：按 channels.py 模块 docstring 的示例配置环境变量。")
        return 2

    targets = [c for c in configs if args.channel is None or c.id == args.channel]
    if not targets:
        print(f"✗ 没有 id 为 {args.channel!r} 的通道；已配置：{[c.id for c in configs]}")
        return 2

    codes = [_probe(c, args.query) for c in targets]
    print(f"\n{'=' * 62}")
    worst = max(codes) if codes else 0
    print("结果：" + ("全部通道契约吻合" if worst == 0 else "存在不符，按上面的提示修正后重跑"))
    print("=" * 62)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
