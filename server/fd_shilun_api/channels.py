"""中文文献授权通道（task 2.1/2.2）：合规边界固化在代码结构里。

规格（spec shilun-serious-writing「论文检索」）：
- 中文文献检索按「已授权通道优先、开放元数据兜底」执行；本模块只负责前者；
- 没有可用授权通道时本模块返回 reason=no_authorized_channel，消费面
  （search.py）据此走开放元数据兜底并标注覆盖不完整，不把该原因码透给用户；
- 原因码 no_authorized_channel 只保留给需要原文全文的能力；
- SHALL NOT 以浏览器自动化抓取商业文献库替代授权通道。

本模块的边界：
- 从不启动浏览器、从不抓取付费文献库页面；只按配置调用已授权厂商的
  HTTP 接口，或用测试/演示用的沙箱适配器。
- 未配置、配置损坏、凭据缺失一律降级为「没有可用授权通道」，由调用方
  显式处理；不绕过、不静默、不回退到抓取。

配置：环境变量 FD_SHILUN_CN_CHANNELS 为 JSON 数组，数组顺序即通道优先级。
每项字段：

    id            通道标识（必填，写入 provenance.source）
    label         展示名（缺省用 id）
    kind          "http" 或 "sandbox"（必填）
    license       授权依据/合同号（必填；写入 provenance.license 与
                  provenance.authorization.basis——没有授权依据就不是授权通道）
    base_url      HTTP 检索端点（kind=http 必填，POST JSON）
    api_key_env   存放 API key 的环境变量名（kind=http 必填）。变量未设置
                  不再等于通道不可用：该通道进入「凭据待提供」状态，可由调用方
                  按请求提供凭据（BYOK，见下）
    auth_scheme   "bearer"（默认）或 "header"
    header_name   auth_scheme=header 时的请求头名（必填）
    fixtures      kind=sandbox 时的内联样本（见下）

BYOK 凭据：调用方以 {通道 id: key} 显式传入，优先级高于环境变量，只在单次
调用的内存中存活——不写入 ChannelConfig、不写结果、不进日志、不回显。因此
凭据可以按租户/按请求提供，产品无需持久化任何厂商密钥。

示例：

    [
      {"id": "wanfang", "label": "万方数据", "kind": "http",
       "base_url": "https://api.example.com/v1/search",
       "api_key_env": "WANFANG_API_KEY", "license": "授权依据/合同号",
       "auth_scheme": "header", "header_name": "X-API-KEY"},
      {"id": "sandbox-cn", "label": "沙箱", "kind": "sandbox",
       "license": "测试样本，不构成对外授权",
       "fixtures": [{"query_substring": "乡村振兴",
                     "records": [{"标识": "CN-001", "题名": "…",
                                  "作者": ["张三"], "年份": 2023,
                                  "来源": "某学报", "摘要": "…",
                                  "doi": "10.1234/x",
                                  "url": "https://example.org/x"}]}]}
    ]

HTTP 响应契约（假定，尚未与厂商正式签约）：POST base_url，请求体
{"query", "page", "per_page"}，响应可为顶层 {"results": [...]}、
{"items": [...]}，或 {"data": {"records": [...]}}；total 取顶层
total/count、meta.total/meta.count 或 data.total。记录字段接受
stable_id/doi/title/authors/year/venue/abstract/url 及中文别名
（标识/题名/标题/作者/年份/来源/摘要/链接）。
注意：该契约必须在上线前与厂商官方 API 文档逐字段复核；当前解析是防御
性的，认不出的响应判为 upstream，绝不猜测语义。

沙箱 fixtures 用大小写不敏感的子串匹配，取第一个命中的样本；page/per_page
在样本内切片。它用于无厂商账号时证明「已授权路径」可跑通，不代表真实数据。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

try:  # httpx 目前只在 dev 依赖里；生产镜像若缺它，HTTP 通道降级为不可用
    import httpx
except ImportError:  # pragma: no cover - 仅在未安装 httpx 的环境触发
    httpx = None  # type: ignore[assignment]

ENV_CHANNELS = "FD_SHILUN_CN_CHANNELS"
_TIMEOUT_SECONDS = 10.0
_MAX_PER_PAGE = 25

Failure = Literal["quota", "upstream", "unreachable", "not_indexed"]

# 多通道全部失败时的归因优先级：配额/限流最能指导运维，其次上游、不可达；
# 所有通道都只是「无命中」时才报 not_indexed（task 2.3 的归因要求）。
_FAILURE_PRIORITY: dict[str, int] = {
    "quota": 0,
    "upstream": 1,
    "unreachable": 2,
    "not_indexed": 3,
}


# ------------------------------------------------------------------ 数据模型


@dataclass(frozen=True)
class ChannelResult:
    """单次通道检索结果。

    records 为结构化元数据（stable_id/doi/title/authors/year/venue/
    abstract/url）；failure 非空时 reason 必填，空结果不得静默。
    """

    records: list[dict[str, Any]]
    total: int | None
    channel: str
    authorization_basis: str
    failure: Failure | None = None
    detail: str | None = None


class SearchChannel(Protocol):
    """检索适配器协议：一个通道 = 一个已授权的数据来源。"""

    id: str
    label: str
    license: str

    def search(
        self, query: str, *, page: int = 1, per_page: int = 10
    ) -> ChannelResult: ...


@dataclass(frozen=True)
class ChannelConfig:
    """通道配置项；problem 非空表示该通道不可用（加载期不抛异常）。"""

    id: str
    label: str
    kind: str
    license: str
    base_url: str | None = None
    api_key_env: str | None = None
    auth_scheme: str = "bearer"
    header_name: str | None = None
    fixtures: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    problem: str | None = None
    # 结构上合法、只是本机没有凭据：可由调用方按请求提供（BYOK）。
    # 与 problem 区分——它不是配置错误，不该被当成「通道写坏了」。
    credential_pending: bool = False

    @property
    def usable(self) -> bool:
        """结构可用（不看凭据）；能否真正发请求由 _resolve_api_key 决定。"""
        return self.problem is None


class ChannelConfigError(ValueError):
    """通道配置错误；加载器把它降级为「不可用」，不向调用方抛出。"""


# ---------------------------------------------------------------- 字段归一化


def _text(value: object) -> str | None:
    """尽力把标量/带名字典收敛成非空字符串，认不出就返回 None。"""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        for key in ("name", "title", "display_name", "value"):
            got = _text(value.get(key))
            if got:
                return got
    return None


def _raw_first(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _first_text(raw: Mapping[str, Any], *keys: str) -> str | None:
    return _text(_raw_first(raw, *keys))


def _authors(value: object) -> list[str]:
    """作者既可能是数组（含 dict），也可能是「张三;李四」这类字符串。"""
    if isinstance(value, str):
        parts = re.split(r"[;,，；、]", value)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            name = _text(item)
            if name:
                out.append(name)
        return out
    name = _text(value)
    return [name] if name else []


def _year(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d{4}", value)
        if match:
            return int(match.group())
    return None


def normalize_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把厂商记录映射为固定键名；中英文字段别名都接受。"""
    doi = _first_text(raw, "doi", "DOI")
    url = _first_text(raw, "url", "链接", "地址")
    stable_id = (
        _first_text(raw, "stable_id", "id", "标识", "记录id", "文献id")
        or doi
        or url
        or ""
    )
    return {
        "stable_id": stable_id,
        "doi": doi,
        "title": _first_text(raw, "title", "题名", "标题"),
        "authors": _authors(_raw_first(raw, "authors", "作者")),
        "year": _year(_raw_first(raw, "year", "年份", "publication_year", "年")),
        "venue": _first_text(raw, "venue", "来源", "期刊", "source", "journal"),
        "abstract": _first_text(raw, "abstract", "摘要"),
        "url": url,
    }


def _payload_records(
    payload: object,
) -> tuple[list[dict[str, Any]], int | None] | None:
    """从防御性假定的响应形状里取出记录列表与 total。"""
    if not isinstance(payload, dict):
        return None
    candidates: list[object] = [payload.get("results"), payload.get("items")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("records"), data.get("items"), data.get("results")])
    rows = next((item for item in candidates if isinstance(item, list)), None)
    if rows is None:
        return None
    records = [normalize_record(row) for row in rows if isinstance(row, dict)]
    return records, _payload_total(payload, data)


def _payload_total(payload: dict[str, Any], data: object) -> int | None:
    containers = [payload] + ([data] if isinstance(data, dict) else [])
    for container in containers:
        for key in ("total", "count"):
            value = container.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
        meta = container.get("meta")
        if isinstance(meta, dict):
            for key in ("total", "count"):
                value = meta.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
    return None


# -------------------------------------------------------------- HTTP 适配器


class HttpSearchChannel:
    """按授权配置调用厂商 HTTP 检索接口；不渲染页面、不抓取网页。"""

    kind = "http"

    def __init__(
        self,
        config: ChannelConfig,
        *,
        client: "httpx.Client | None" = None,
        api_key: str | None = None,
    ) -> None:
        if config.kind != "http":
            raise ChannelConfigError(f"通道 {config.id} 不是 http 类型")
        if not config.usable:
            # 授权在配置层就成立；不可用配置绝不允许构造出可发请求的适配器
            raise ChannelConfigError(config.problem or f"通道 {config.id} 配置不可用")
        if httpx is None:
            raise ChannelConfigError("未安装 httpx，HTTP 通道不可用")
        if not config.base_url:
            raise ChannelConfigError(f"通道 {config.id} 缺少 base_url")
        key = api_key if api_key is not None else os.environ.get(
            config.api_key_env or "", ""
        )
        key = (key or "").strip()
        if not key:
            raise ChannelConfigError(
                f"通道 {config.id} 的凭据环境变量 {config.api_key_env or '（未配置）'} 未设置"
            )
        self.config = config
        self.id = config.id
        self.label = config.label
        self.license = config.license
        self._api_key = key
        self._client = client

    def _headers(self) -> dict[str, str]:
        if self.config.auth_scheme == "header":
            return {self.config.header_name or "X-API-KEY": self._api_key}
        return {"Authorization": f"Bearer {self._api_key}"}

    def _result(
        self,
        *,
        records: Sequence[dict[str, Any]] = (),
        total: int | None = None,
        failure: Failure | None = None,
        detail: str | None = None,
    ) -> ChannelResult:
        return ChannelResult(
            records=list(records),
            channel=self.id,
            authorization_basis=self.license,
            total=total,
            failure=failure,
            detail=detail,
        )

    def search(
        self, query: str, *, page: int = 1, per_page: int = 10
    ) -> ChannelResult:
        body = {"query": query, "page": page, "per_page": per_page}
        try:
            if self._client is not None:
                response = self._client.post(
                    self.config.base_url or "", json=body, headers=self._headers()
                )
            else:
                with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
                    response = client.post(
                        self.config.base_url or "", json=body, headers=self._headers()
                    )
        except httpx.TimeoutException as exc:
            return self._result(
                failure="unreachable",
                detail=f"请求超时（{_TIMEOUT_SECONDS:.0f}s）: {type(exc).__name__}",
            )
        except httpx.HTTPError as exc:
            return self._result(
                failure="unreachable", detail=f"网络错误: {type(exc).__name__}: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 —— 适配器异常也必须显式归因
            return self._result(
                failure="upstream", detail=f"适配器异常: {type(exc).__name__}: {exc}"
            )
        return self._parse_response(response)

    def _parse_response(self, response: "httpx.Response") -> ChannelResult:
        status = response.status_code
        if status == 429:
            return self._result(failure="quota", detail="HTTP 429（配额/限流）")
        if status in (401, 403):
            return self._result(
                failure="upstream",
                detail=f"HTTP {status}（鉴权失败：核对 api_key_env 与授权范围）",
            )
        if status >= 500:
            return self._result(failure="upstream", detail=f"HTTP {status}（上游异常）")
        if status >= 400:
            return self._result(failure="upstream", detail=f"HTTP {status}（请求被拒绝）")
        try:
            payload = response.json()
        except ValueError as exc:
            return self._result(
                failure="upstream",
                detail=f"响应不是合法 JSON: {type(exc).__name__}: {exc}",
            )
        parsed = _payload_records(payload)
        if parsed is None:
            return self._result(
                failure="upstream",
                detail="响应缺少 results/items/data.records 列表",
            )
        records, total = parsed
        if not records:
            return self._result(
                failure="not_indexed", total=total, detail="授权通道无命中"
            )
        return self._result(records=records, total=total)


# ------------------------------------------------------------ 沙箱适配器


class SandboxSearchChannel:
    """确定性的进程内适配器：用内联样本证明授权路径，不发任何网络请求。"""

    kind = "sandbox"

    def __init__(self, config: ChannelConfig) -> None:
        if config.kind != "sandbox":
            raise ChannelConfigError(f"通道 {config.id} 不是 sandbox 类型")
        fixtures = tuple(item for item in config.fixtures if isinstance(item, dict))
        if not fixtures:
            raise ChannelConfigError(f"沙箱通道 {config.id} 没有 fixtures")
        self.config = config
        self.id = config.id
        self.label = config.label
        self.license = config.license
        self._fixtures = fixtures

    def _result(
        self,
        *,
        records: Sequence[dict[str, Any]] = (),
        total: int | None = None,
        failure: Failure | None = None,
        detail: str | None = None,
    ) -> ChannelResult:
        return ChannelResult(
            records=list(records),
            channel=self.id,
            authorization_basis=self.license,
            total=total,
            failure=failure,
            detail=detail,
        )

    def _fixture(self, query: str) -> Mapping[str, Any] | None:
        needle = query.casefold()
        for fixture in self._fixtures:
            sub = _text(fixture.get("query_substring"))
            if sub and sub.casefold() in needle:
                return fixture
        return None

    def search(
        self, query: str, *, page: int = 1, per_page: int = 10
    ) -> ChannelResult:
        fixture = self._fixture(query)
        if fixture is None:
            return self._result(
                failure="not_indexed",
                detail=f"沙箱未配置匹配 {query!r} 的样本",
            )
        raw_rows = fixture.get("records")
        rows = (
            [normalize_record(row) for row in raw_rows if isinstance(row, dict)]
            if isinstance(raw_rows, list)
            else []
        )
        total = len(rows)
        page = max(int(page), 1)
        per_page = min(max(int(per_page), 1), _MAX_PER_PAGE)
        start = (page - 1) * per_page
        window = rows[start : start + per_page]
        if not window:
            detail = (
                f"沙箱请求页超出命中范围（total={total}）"
                if total
                else f"沙箱样本无记录: {fixture.get('query_substring')!r}"
            )
            return self._result(failure="not_indexed", total=total, detail=detail)
        return self._result(records=window, total=total)


# ---------------------------------------------------------------- 配置加载


def _parse_fixtures(raw: object, problems: list[str]) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list) or not raw:
        problems.append("sandbox 通道缺少非空 fixtures 列表")
        return ()
    fixtures: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"fixtures 第 {index} 项不是 JSON 对象")
            continue
        sub = _text(item.get("query_substring"))
        records = item.get("records")
        if not sub or not isinstance(records, list):
            problems.append(
                f"fixtures 第 {index} 项缺少 query_substring 或 records 列表"
            )
            continue
        fixtures.append(
            {
                "query_substring": sub,
                "records": [row for row in records if isinstance(row, dict)],
            }
        )
    if raw and not fixtures:
        problems.append("fixtures 中没有可用样本")
    return tuple(fixtures)


def _parse_entry(index: int, raw: object) -> ChannelConfig:
    """解析单项配置；任何缺失/非法字段都记入 problem，绝不抛异常。"""
    if not isinstance(raw, dict):
        return ChannelConfig(
            id=f"invalid-{index}",
            label=f"invalid-{index}",
            kind="",
            license="",
            problem=f"第 {index} 项不是 JSON 对象",
        )

    problems: list[str] = []
    raw_id = _text(raw.get("id"))
    channel_id = raw_id or f"channel-{index}"
    if not raw_id:
        problems.append("缺少 id")
    label = _text(raw.get("label")) or channel_id
    kind = _text(raw.get("kind")) or ""
    if kind not in ("http", "sandbox"):
        problems.append(f'kind 必须是 "http" 或 "sandbox"（当前 {kind or "空"}）')
    basis = _text(raw.get("license")) or ""
    if not basis:
        problems.append("缺少 license（授权依据/合同号）")

    base_url: str | None = None
    api_key_env: str | None = None
    header_name: str | None = None
    auth_scheme = _text(raw.get("auth_scheme")) or "bearer"
    fixtures: tuple[dict[str, Any], ...] = ()
    credential_pending = False

    if kind == "http":
        base_url = _text(raw.get("base_url"))
        if not base_url:
            problems.append("HTTP 通道缺少 base_url")
        elif not base_url.lower().startswith(("http://", "https://")):
            problems.append("base_url 必须以 http:// 或 https:// 开头")
        api_key_env = _text(raw.get("api_key_env"))
        if not api_key_env:
            problems.append("HTTP 通道缺少 api_key_env")
        elif not os.environ.get(api_key_env, "").strip():
            # BYOK：环境变量没设不是配置错误，只是这次调用得由使用者提供凭据。
            # 凭据值绝不进 config——config 会被序列化/展示，只留变量名。
            credential_pending = True
        if auth_scheme not in ("bearer", "header"):
            problems.append('auth_scheme 必须是 "bearer" 或 "header"')
        elif auth_scheme == "header":
            header_name = _text(raw.get("header_name"))
            if not header_name:
                problems.append("auth_scheme=header 时必须提供 header_name")
        if httpx is None:
            problems.append("未安装 httpx，HTTP 通道不可用")
    elif kind == "sandbox":
        fixtures = _parse_fixtures(raw.get("fixtures"), problems)

    return ChannelConfig(
        id=channel_id,
        label=label,
        kind=kind,
        license=basis,
        base_url=base_url,
        api_key_env=api_key_env,
        auth_scheme=auth_scheme,
        header_name=header_name,
        fixtures=fixtures,
        problem="；".join(problems) or None,
        credential_pending=credential_pending,
    )


@dataclass(frozen=True)
class _ConfigLoad:
    configs: list[ChannelConfig]
    problems: list[str]


def _load_config(raw: str | None = None) -> _ConfigLoad:
    """读取配置；任何错误收敛为 problems，导入/请求期都不抛异常。"""
    if raw is None:
        raw = os.environ.get(ENV_CHANNELS, "")
    text = (raw or "").strip()
    if not text:
        return _ConfigLoad([], [])
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        return _ConfigLoad([], [f"{ENV_CHANNELS} 不是合法 JSON: {exc}"])
    if not isinstance(parsed, list):
        return _ConfigLoad([], [f"{ENV_CHANNELS} 必须是 JSON 数组"])

    configs: list[ChannelConfig] = []
    problems: list[str] = []
    for index, item in enumerate(parsed, start=1):
        try:
            config = _parse_entry(index, item)
        except Exception as exc:  # noqa: BLE001 —— 单项配置错误不得拖垮服务
            config = ChannelConfig(
                id=f"channel-{index}",
                label=f"channel-{index}",
                kind="",
                license="",
                problem=f"第 {index} 项解析失败: {type(exc).__name__}: {exc}",
            )
        configs.append(config)
        if config.problem:
            problems.append(f"{config.id}: {config.problem}")
    return _ConfigLoad(configs, problems)


def _resolve_api_key(
    config: ChannelConfig, credentials: Mapping[str, str] | None
) -> str | None:
    """凭据取值优先级：显式提供（BYOK）> 环境变量；空串一律视为未提供。

    返回的明文只在本次调用内存中存活：不写 config、不写结果、不进日志。
    """
    explicit = ""
    if credentials:
        explicit = str(credentials.get(config.id) or "").strip()
    if explicit:
        return explicit
    env_name = config.api_key_env or ""
    if env_name:
        from_env = os.environ.get(env_name, "").strip()
        if from_env:
            return from_env
    return None


def _build_channel(
    config: ChannelConfig,
    *,
    credentials: Mapping[str, str] | None = None,
) -> tuple[SearchChannel | None, str | None]:
    if not config.usable:
        return None, config.problem
    try:
        if config.kind == "http":
            key = _resolve_api_key(config, credentials)
            if key is None:
                # 只报变量名/通道 id，绝不回显凭据值（这里本来也没有值）
                return None, (
                    f"通道 {config.id} 凭据待提供（BYOK）：显式凭据与环境变量 "
                    f"{config.api_key_env or '（未配置）'} 均为空"
                )
            return HttpSearchChannel(config, api_key=key), None
        if config.kind == "sandbox":
            return SandboxSearchChannel(config), None
    except ChannelConfigError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 —— 构造失败同样降级为不可用
        return None, f"通道构造失败: {type(exc).__name__}: {exc}"
    return None, f"未知通道类型 {config.kind!r}"


def configured_channels(raw: str | None = None) -> list[ChannelConfig]:
    """全部已解析的配置项（含不可用项，problem 说明原因），按配置顺序。"""
    return _load_config(raw).configs


def usable_channels(
    raw: str | None = None,
    *,
    credentials: Mapping[str, str] | None = None,
) -> list[SearchChannel]:
    """可用通道适配器，按配置顺序；没有则空列表。

    `credentials` 是使用者自备的凭据（通道 id → key），优先于环境变量；
    缺凭据的通道不构造适配器，因此这里的返回即「本次调用真能发请求的通道」。
    """
    channels: list[SearchChannel] = []
    for config in _load_config(raw).configs:
        channel, _ = _build_channel(config, credentials=credentials)
        if channel is not None:
            channels.append(channel)
    return channels


def has_authorized_channel(
    raw: str | None = None,
    *,
    credentials: Mapping[str, str] | None = None,
) -> bool:
    """是否存在至少一个本次调用可用的授权通道（凭据齐全）。"""
    return bool(usable_channels(raw, credentials=credentials))


def channel_from_config(
    config: ChannelConfig,
    *,
    credentials: Mapping[str, str] | None = None,
) -> SearchChannel | None:
    """由单个配置项构造适配器；不可用、缺凭据或构造失败返回 None。"""
    channel, _ = _build_channel(config, credentials=credentials)
    return channel


# ---------------------------------------------------------------- 检索入口


def _empty_payload(reason: str, detail: str) -> dict[str, Any]:
    return {
        "records": [],
        "total": None,
        "reason": reason,
        "detail": detail,
        "channel": None,
        "authorization_basis": None,
    }


def _resolve_channels(
    raw: str | None = None,
    *,
    credentials: Mapping[str, str] | None = None,
) -> tuple[list[SearchChannel], list[str], list[str]]:
    """→ (适配器, 配置级问题, 仅缺凭据的通道说明)。

    「缺凭据」与「配置写坏」分开报：前者是 BYOK 的正常状态，调用方补凭据即可，
    不该被描述成配置错误。
    """
    load = _load_config(raw)
    channels: list[SearchChannel] = []
    problems = list(load.problems)
    pending: list[str] = []
    for config in load.configs:
        if not config.usable:
            continue
        channel, problem = _build_channel(config, credentials=credentials)
        if channel is not None:
            channels.append(channel)
        elif config.kind == "http" and _resolve_api_key(config, credentials) is None:
            pending.append(f"{config.id}（环境变量 {config.api_key_env or '未配置'}）")
        else:
            problems.append(f"{config.id}: {problem}")
    return channels, problems, pending


def _no_channel_detail(problems: Sequence[str], pending: Sequence[str] = ()) -> str:
    parts = list(problems)
    if pending:
        parts.append("以下通道等待凭据（可由调用方按请求提供）：" + "、".join(pending))
    if not parts:
        return f"未配置 {ENV_CHANNELS}（无已授权的合规中文文献通道）"
    return "无可用授权通道：" + "；".join(parts)


def _failure_detail(failures: Sequence[tuple[str, str, str | None]]) -> str:
    parts: list[str] = []
    for channel_id, failure, detail in failures:
        piece = f"{channel_id}: {failure}"
        if detail:
            piece += f"（{detail}）"
        parts.append(piece)
    return "所有授权通道均未返回结果：" + "；".join(parts)


def search_chinese(
    query: str,
    *,
    page: int = 1,
    per_page: int = 10,
    channels: Sequence[SearchChannel] | None = None,
    credentials: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """中文文献检索：按配置顺序尝试授权通道，返回统一结果字典。

    成功时 reason=None，channel/authorization_basis 记录来源与授权依据；
    没有任何可用通道时 reason="no_authorized_channel"（本模块只表达「授权通道
    这一层没有可用的」；消费面会在此基础上走开放元数据兜底，不把该原因码透给
    用户——见 search.py）。通道全部失败时按
    quota > upstream > unreachable > not_indexed 取最有信息量的归因。

    `credentials` 为使用者自备凭据（通道 id → key），优先于环境变量；仅在本
    次调用内存中存活，不写入配置、结果或日志。
    """
    query = (query or "").strip()
    page = max(int(page), 1)
    per_page = min(max(int(per_page), 1), _MAX_PER_PAGE)

    if channels is None:
        adapters, problems, pending = _resolve_channels(credentials=credentials)
    else:
        adapters = list(channels)
        problems = [] if adapters else ["调用方未提供任何授权通道"]
        pending = []

    if not adapters:
        return _empty_payload("no_authorized_channel", _no_channel_detail(problems, pending))

    failures: list[tuple[str, str, str | None]] = []
    for channel in adapters:
        try:
            result = channel.search(query, page=page, per_page=per_page)
        except Exception as exc:  # noqa: BLE001 —— 适配器异常也显式归因，不静默
            failures.append(
                (
                    getattr(channel, "id", "unknown"),
                    "upstream",
                    f"适配器异常: {type(exc).__name__}: {exc}",
                )
            )
            continue
        if result.records:
            return {
                "records": result.records,
                "total": result.total,
                "reason": None,
                "detail": None,
                "channel": result.channel or getattr(channel, "id", ""),
                "authorization_basis": result.authorization_basis
                or getattr(channel, "license", ""),
            }
        failures.append(
            (
                result.channel or getattr(channel, "id", "unknown"),
                result.failure or "not_indexed",
                result.detail,
            )
        )

    reason = min(
        failures,
        key=lambda item: _FAILURE_PRIORITY.get(item[1], len(_FAILURE_PRIORITY)),
    )[1]
    return _empty_payload(reason, _failure_detail(failures))
