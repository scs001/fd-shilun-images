"""个人层访问控制（task 3.3）：必须携带用户授权，未授权拒绝并留痕。

两种令牌形态（都算「用户授权」）：
- JWKS JWT（RS256）：设 FD_SHILUN_AUTH_JWKS（如 Logto 的 JWKS 端点，6.3 接线）
  + FD_SHILUN_AUTH_AUDIENCE 时启用，sub 即 user_id。
- 开发态固定令牌：FD_SHILUN_AUTH_DEV_TOKENS="token1=user_a,token2=user_b"
  （本地联调与测试用；生产不设此变量即只剩 JWT 一条路）。
"""

from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request

from . import personal


class Unauthorized(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=401, detail=detail)


def _dev_tokens() -> dict[str, str]:
    raw = os.environ.get("FD_SHILUN_AUTH_DEV_TOKENS", "")
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            token, user = pair.split("=", 1)
            out[token.strip()] = user.strip()
    return out


def _verify_jwt(token: str) -> str:
    import jwt  # pyjwt

    jwks_url = os.environ.get("FD_SHILUN_AUTH_JWKS", "")
    audience = os.environ.get("FD_SHILUN_AUTH_AUDIENCE", "")
    if not jwks_url:
        raise Unauthorized("JWT 校验未配置（FD_SHILUN_AUTH_JWKS）")
    from jwt import PyJWKClient

    jwks = PyJWKClient(jwks_url)
    signing = jwks.get_signing_key_from_jwt(token)
    # Logto 1.42 签发 ES384（实测 aloadtree ID token）；RS256 保留兼容自签场景。
    # 未配置 audience 时显式关 aud 校验：pyjwt 2.10+ 对「token 带 aud 但调用方未传」
    # 也执行校验并抛 InvalidAudienceError（线上实测第二个坑）。
    claims = jwt.decode(
        token,
        signing.key,
        algorithms=["RS256", "ES384"],
        audience=audience or None,
        options={
            "require": ["exp", "sub"],
            **({} if audience else {"verify_aud": False}),
        },
    )
    return str(claims["sub"])


def authenticate(request: Request) -> str:
    """从 Authorization: Bearer 解出 user_id；失败即 401 并写审计留痕。"""
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""

    user_id: str | None = None
    deny_reason = "missing bearer token"
    if token:
        dev = _dev_tokens()
        if token in dev:
            user_id = dev[token]
        else:
            try:
                user_id = _verify_jwt(token)
            except Exception as exc:  # noqa: BLE001 —— 任何校验失败都归一为 401
                deny_reason = f"invalid token: {type(exc).__name__}"

    conn = personal.connect()
    try:
        personal.migrate(conn)
        if user_id:
            personal.audit(conn, user_id, "auth", request.url.path, "allowed")
            return user_id
        personal.audit(conn, None, "auth", request.url.path, "denied", detail=deny_reason)
        raise Unauthorized(deny_reason)
    finally:
        conn.close()


def require_user(user_id: str = Depends(authenticate)) -> str:
    return user_id
