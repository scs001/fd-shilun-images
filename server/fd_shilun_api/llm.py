"""LLM 网关客户端（task 4.1/4.2 的可选增强层）。

OpenAI 兼容网关（默认 token.finddatatech.cloud）：
- 不设 FD_SHILUN_LLM_API_KEY → chat() 返回 None，调用方走确定性回退；
- 设了 KEY → POST {base}/chat/completions，超时 20s；
- 任何失败（网络/非 2xx/坏负载）抛 LLMError，由调用方捕获降级。

铁律：不自研重试堆栈，失败就降级。
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_BASE_URL = "https://token.finddatatech.cloud/v1"
DEFAULT_MODEL = "gpt-5.4-mini"


class LLMError(RuntimeError):
    """网关调用失败（网络/HTTP/负载解析）。调用方捕获后降级，不重试。"""


def is_configured() -> bool:
    return bool(os.environ.get("FD_SHILUN_LLM_API_KEY", "").strip())


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 900,
    timeout: float = 20.0,
) -> str | None:
    """单次补全调用。无 KEY → None；失败 → LLMError（不重试）。"""
    key = os.environ.get("FD_SHILUN_LLM_API_KEY", "").strip()
    if not key:
        return None
    import httpx  # 延迟导入：环境缺 httpx 时仅 LLM 增强不可用，确定性层不受影响

    base = os.environ.get("FD_SHILUN_LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    payload: dict[str, Any] = {
        "model": model or os.environ.get("FD_SHILUN_LLM_MODEL", DEFAULT_MODEL),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = httpx.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
        return content.strip()
    except Exception as exc:  # noqa: BLE001 —— 单次失败即抛，绝不重试
        raise LLMError(f"LLM 网关调用失败: {type(exc).__name__}: {exc}") from exc
