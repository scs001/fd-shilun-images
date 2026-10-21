"""中文通道契约核查脚本测试（deploy/COMPLIANCE.md §1）。

核查脚本的价值在于「用一条命令把假定的厂商契约验成真的」。因此测试要证明：
- 未配置通道时明确失败（退出码 2），不假装通过；
- 契约吻合时退出码 0，且逐字段解析正确；
- 形状不符时退出码非 0 并指出该改哪里；
- 关键字段缺失时退出码非 0。

用本地 HTTP 桩模拟厂商，不访问外网。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_checker() -> ModuleType:
    """按路径加载核查脚本（避开 site-packages 里的同名 scripts 包）。"""
    path = ROOT / "scripts" / "check_cn_channel.py"
    spec = importlib.util.spec_from_file_location("fd_shilun_check_cn_channel", path)
    assert spec and spec.loader, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _serve(payload: dict, status: int = 200) -> tuple[HTTPServer, str]:
    """起一个只返回固定 JSON 的本地桩，返回 (server, base_url)。"""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # 静默
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/search"


def _configure(monkeypatch, base_url: str) -> None:
    monkeypatch.setenv(
        "FD_SHILUN_CN_CHANNELS",
        json.dumps(
            [
                {
                    "id": "stub",
                    "label": "本地桩",
                    "kind": "http",
                    "license": "测试合同-001",
                    "base_url": base_url,
                    "api_key_env": "STUB_KEY",
                    "auth_scheme": "header",
                    "header_name": "X-API-KEY",
                }
            ]
        ),
    )
    monkeypatch.setenv("STUB_KEY", "test-key")


GOOD_RECORD = {
    "id": "CN-001",
    "题名": "乡村振兴战略实施路径研究",
    "作者": ["张三", "李四"],
    "年份": 2023,
    "来源": "某学报",
    "摘要": "本文研究乡村振兴。",
    "doi": "10.1234/x",
}


def test_no_channel_configured_exits_2(monkeypatch) -> None:
    monkeypatch.delenv("FD_SHILUN_CN_CHANNELS", raising=False)
    assert checker.main([]) == 2


def test_matching_contract_exits_0(monkeypatch, capsys) -> None:
    server, url = _serve({"results": [GOOD_RECORD], "total": 1})
    try:
        _configure(monkeypatch, url)
        code = checker.main(["--query", "乡村振兴"])
        out = capsys.readouterr().out
    finally:
        server.shutdown()
    assert code == 0, out
    assert "契约吻合" in out
    assert "乡村振兴战略实施路径研究" in out
    assert "测试合同-001" in out


def test_unknown_shape_exits_nonzero(monkeypatch, capsys) -> None:
    """厂商返回我们不认识的形状时必须报错，不能静默当无命中。"""
    server, url = _serve({"hits": [GOOD_RECORD]})
    try:
        _configure(monkeypatch, url)
        code = checker.main([])
        out = capsys.readouterr().out
    finally:
        server.shutdown()
    assert code != 0, out
    assert "顶层形状未被识别" in out
    assert "_payload_records" in out, "报错必须指出该改哪一处"


def test_missing_critical_field_exits_nonzero(monkeypatch, capsys) -> None:
    """缺 stable_id 与 title 时不能算契约吻合。"""
    server, url = _serve({"results": [{"年份": 2023}]})
    try:
        _configure(monkeypatch, url)
        code = checker.main([])
        out = capsys.readouterr().out
    finally:
        server.shutdown()
    assert code != 0, out
    assert "关键字段解析为空" in out


def test_http_error_exits_nonzero(monkeypatch, capsys) -> None:
    server, url = _serve({"error": "forbidden"}, status=403)
    try:
        _configure(monkeypatch, url)
        code = checker.main([])
        out = capsys.readouterr().out
    finally:
        server.shutdown()
    assert code != 0, out
    assert "请求失败" in out


def test_missing_api_key_exits_2(monkeypatch, capsys) -> None:
    """凭据缺失时无法发请求（BYOK 可补），核查必须拒绝而不是放行。"""
    server, url = _serve({"results": [GOOD_RECORD]})
    try:
        _configure(monkeypatch, url)
        monkeypatch.delenv("STUB_KEY", raising=False)
        code = checker.main([])
        out = capsys.readouterr().out
    finally:
        server.shutdown()
    assert code == 2, out
    assert "未设置或为空" in out
    assert "STUB_KEY" in out, "报错必须点名是哪个环境变量"


def test_sandbox_channel_is_not_accepted_as_authorized(monkeypatch, capsys) -> None:
    """sandbox 通道不构成对外授权，核查必须拒绝而不是放行。"""
    monkeypatch.setenv(
        "FD_SHILUN_CN_CHANNELS",
        json.dumps(
            [
                {
                    "id": "sandbox",
                    "kind": "sandbox",
                    "license": "测试样本，不构成对外授权",
                    "fixtures": [{"query_substring": "乡村振兴", "records": [GOOD_RECORD]}],
                }
            ]
        ),
    )
    code = checker.main([])
    out = capsys.readouterr().out
    assert code == 2, out
    assert "不构成对外授权" in out
