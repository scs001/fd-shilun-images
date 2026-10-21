"""守卫测试（task 2.4）：中文文献通道不得以浏览器自动化抓取商业文献库。

spec shilun-serious-writing「论文检索」：中文文献 SHALL 仅经已授权的合规
通道接入，SHALL NOT 以浏览器自动化抓取商业文献库替代授权通道。

测试解析源码 AST（不是 grep 文本），确保：
- fd_shilun_api/ 与 scripts/ 下没有浏览器自动化库的导入/动态导入；
- 没有 chromedriver/geckodriver 的 subprocess/os.system 调用；
- channels.py 不 import subprocess、不调用 os.system，且网络客户端只有 httpx。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARDED_DIRS = (ROOT / "fd_shilun_api", ROOT / "scripts")

FORBIDDEN_MODULES = {
    "playwright",
    "selenium",
    "pyppeteer",
    "puppeteer",
    "splash",
    "undetected_chromedriver",
    "webdriver",
    "helium",
}
DRIVER_MARKERS = ("chromedriver", "geckodriver")
NETWORK_CLIENTS = {
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "urllib",
    "http",
    "socket",
    "websocket",
    "grpc",
}


def _guard_files() -> list[Path]:
    files = [path for directory in GUARDED_DIRS if directory.exists()
             for path in directory.rglob("*.py")]
    return sorted(files)


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.AST) -> Iterator[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.module, node.lineno


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _string_constants(node: ast.AST) -> Iterator[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value


def _dynamic_import_names(tree: ast.AST) -> Iterator[tuple[str, int]]:
    """importlib.import_module("playwright...") / __import__("selenium...")。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name not in {"importlib.import_module", "__import__"}:
            continue
        for arg in node.args[:1]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield arg.value, node.lineno


@pytest.mark.parametrize("path", _guard_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_browser_automation_imports(path: Path) -> None:
    tree = _parse(path)
    problems: list[str] = []
    for module, lineno in list(_imports(tree)) + list(_dynamic_import_names(tree)):
        root = module.split(".")[0]
        if root in FORBIDDEN_MODULES:
            problems.append(f"{path}:{lineno} 导入了浏览器自动化模块 {module!r}")
    assert not problems, "\n".join(problems)


def test_no_browser_driver_subprocess_invocations() -> None:
    problems: list[str] = []
    for path in _guard_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func) or ""
            if not (name.startswith("subprocess.") or name in {"os.system", "os.popen"}):
                continue
            haystack = " ".join(_string_constants(node)).lower()
            for marker in DRIVER_MARKERS:
                if marker in haystack:
                    problems.append(
                        f"{path}:{node.lineno} 调用 {name} 且参数含 {marker!r}"
                    )
    assert not problems, "\n".join(problems)


def test_channels_module_has_no_subprocess_or_shell_escape() -> None:
    path = ROOT / "fd_shilun_api" / "channels.py"
    tree = _parse(path)

    imported = {module.split(".")[0] for module, _ in _imports(tree)}
    assert "subprocess" not in imported, "channels.py 不得依赖 subprocess"

    shell_calls = [
        f"{path}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (_call_name(node.func) or "") in {"os.system", "os.popen"}
    ]
    assert not shell_calls, "\n".join(shell_calls)


def test_channels_module_only_network_client_is_httpx() -> None:
    path = ROOT / "fd_shilun_api" / "channels.py"
    tree = _parse(path)
    imported = {module.split(".")[0] for module, _ in _imports(tree)}
    used_network_clients = imported & NETWORK_CLIENTS
    assert used_network_clients == {"httpx"}, (
        f"channels.py 只允许 httpx 作为网络客户端，实际导入: {sorted(used_network_clients)}"
    )
