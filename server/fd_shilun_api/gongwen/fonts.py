"""字体角色、回退链与严格模式（design 决策 4）。

字体是部署环境差异最大的部分：Windows 有仿宋_GB2312，macOS 只有 Songti/STSong，
CI 容器往往一个中文字体都没有。硬编码单一字体名会让渲染在缺字体环境**静默走样**
（Word 用默认宋体替代，用户看不出来），这正是 spec「字体授权与降级」要禁止的。

因此：
- 每个排版角色声明一条**有序**回退链（最后一项是保证可用的兜底族名）；
- 实际选用哪个族由本机已装字体决定，缺字体时按声明顺序回退并**报告**；
- 严格模式（交付前门）缺首选字体即失败，不静默降级；
- 本模块只读字体目录的元数据（family name），**不复制、不打包**任何字体文件——
  商业字体授权未落地前随产品分发字体文件是明确禁止的（spec 字体授权约束）。

family name 的读取优先用 fontTools 解析 .ttf/.otf/.ttc（TTC 取 index 0），
因为字体文件名与真实 family name 经常不一致（simsun.ttc 里可能只有 SimSun/宋体，
方正文件名是 FZXBSJW.TTF 而 family 是「方正小标宋简体」）。fontTools 不可用时
退回文件名→族名别名表，保证最小环境仍能工作。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 角色 → 声明顺序的回退链
# ---------------------------------------------------------------------------

# 依据 GB/T 9704-2012：
# - 标题、发文机关标志：2 号小标宋体字（7.4、8.1）；
# - 正文：3 号仿宋体字（8.3）；
# - 一级标题：3 号黑体；二级标题：3 号楷体（8.3 层次序数惯例）；
# - 页码：4 号半角宋体阿拉伯数字（10）。
# 每链末项是「几乎所有环境都能解析」的兜底族名；即使本机也没装（CI 常见），
# 文档仍写出该族名，由 Word/WPS 用本机替代字体显示，不会写空字体名。
FONT_ROLES: dict[str, tuple[str, ...]] = {
    "title": ("方正小标宋简体", "方正小标宋_GBK", "华文中宋", "宋体"),
    "body": ("仿宋_GB2312", "仿宋", "FangSong", "宋体"),
    "h1": ("黑体", "SimHei", "宋体"),
    "h2": ("楷体_GB2312", "楷体", "KaiTi", "宋体"),
    "issuer_mark": ("方正小标宋简体", "方正小标宋_GBK", "华文中宋", "宋体"),
    "page_number": ("宋体", "SimSun", "serif"),
}

STRICT_ENV = "FD_SHILUN_FONT_STRICT"
FONT_DIRS_ENV = "FD_SHILUN_FONT_DIRS"

# 文件名主干 → 常见 family 名。仅在 fontTools 不可用或解析失败时使用；
# 同一字体在不同发行版可能落成 simfang.ttf / FangSong.ttf / STFANGSO.TTF，
# 因此用「包含」匹配而不是精确相等。
_FILENAME_ALIASES: dict[str, tuple[str, ...]] = {
    "simfang": ("仿宋", "FangSong"),
    "fangsong": ("仿宋", "FangSong"),
    "fs_gb2312": ("仿宋_GB2312",),
    "simhei": ("黑体", "SimHei"),
    "simkai": ("楷体", "KaiTi"),
    "kaiti": ("楷体", "KaiTi"),
    "simsun": ("宋体", "SimSun"),
    "songti": ("宋体", "Songti SC", "STSong"),
    "stsong": ("宋体", "STSong"),
    "fzxbs": ("方正小标宋简体",),
    "fzxiaobiaosong": ("方正小标宋简体",),
    "fzssk": ("方正书宋简体",),
    "msyh": ("微软雅黑",),
    "notosanscjk": ("Noto Sans CJK SC", "Noto Sans CJK"),
    "notoserifcjk": ("Noto Serif CJK SC", "Noto Serif CJK"),
}

_FONT_SUFFIXES = frozenset({".ttf", ".otf", ".ttc", ".otc"})

# 同一款公文字体在不同系统上的 family 名并不相同，精确相等会把「装了」误判成
# 「没装」：macOS 的宋体 family 是「宋体-简」/「Songti SC」，黑体是「STHeiti」，
# Windows 才是「宋体」/「SimHei」。这里把 GB 规范族名映射到各平台实际族名。
# 别名只用于**判断可用性**；写进 DOCX 的仍是规范族名，由 Word/WPS 解析。
_FAMILY_ALIASES: dict[str, tuple[str, ...]] = {
    "宋体": ("宋体-简", "宋体-繁", "Songti SC", "Songti TC", "STSong", "SimSun", "NSimSun"),
    "仿宋": ("仿宋-简", "仿宋-繁", "FangSong", "FangSong SC", "STFangsong", "Fangsong"),
    "黑体": (
        "黑体-简",
        "黑体-繁",
        "Heiti SC",
        "Heiti TC",
        "STHeiti",
        "STHeiti Light",
        "STHeiti Medium",
        "SimHei",
    ),
    "楷体": ("楷体-简", "楷体-繁", "Kaiti SC", "Kaiti TC", "STKaiti", "KaiTi"),
    "华文中宋": ("STZhongsong",),
    "方正小标宋简体": ("方正小标宋_GBK", "FZXiaoBiaoSong-B05S", "FZXBSJW"),
}

# 简繁/地区后缀在判断可用性时抹平：宋体-简 就是宋体。
_NORM_SUFFIXES = ("-简", "-繁", "-sc", "-tc", " sc", " tc")


def _norm_family(name: str) -> str:
    """族名归一：大小写、下划线、空格与简繁后缀都不该影响「装没装」。"""
    text = str(name or "").strip().lower().replace("_", " ")
    changed = True
    while changed:
        changed = False
        for suffix in _NORM_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
    return " ".join(text.split())


def _installed_index(installed: Iterable[str]) -> set[str]:
    return {_norm_family(name) for name in installed}


def family_installed(family: str, installed_norm: set[str]) -> bool:
    """规范族名（或其平台别名）是否可用；`installed_norm` 需已归一。"""
    candidates = (family, *_FAMILY_ALIASES.get(family, ()))
    return any(_norm_family(candidate) in installed_norm for candidate in candidates)


# 进程内缓存：字体目录扫描一次要读上百个字体头，不能每个请求扫一遍。
# key 是目录元组，显式注入（测试）不会串缓存。
_FONT_CACHE: dict[tuple[str, ...], frozenset[str]] = {}
_CACHE_LOCK = threading.Lock()


def default_font_dirs() -> list[Path]:
    """本机应扫描的字体目录：环境变量追加在平台默认值之前。"""
    out: list[Path] = []
    raw = os.environ.get(FONT_DIRS_ENV, "").strip()
    if raw:
        out.extend(Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip())
    out.extend(
        [
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"),
            Path("~/Library/Fonts").expanduser(),
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path("C:/Windows/Fonts"),
        ]
    )
    # 去重保序：环境变量里重复列出的目录不重复扫描
    seen: set[str] = set()
    unique: list[Path] = []
    for path in out:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _alias_from_filename(path: Path) -> set[str]:
    stem = path.stem.lower()
    families: set[str] = set()
    for key, names in _FILENAME_ALIASES.items():
        if key in stem:
            families.update(names)
    return families


def _families_of(path: Path) -> set[str]:
    """读字体文件的全部 family name；失败时退回文件名别名。

    .ttc/.otc 是字体集合（macOS 的 Songti.ttc 里就有 8 个 face，STHeiti 同理），
    只读 face 0 会漏掉绝大多数中文字体，故整个集合逐个展开。
    """
    try:  # fontTools 是可选依赖：只在装了的时候解析真实 family name
        from fontTools.ttLib import TTCollection, TTFont
    except Exception:  # pragma: no cover - 取决于部署环境
        return _alias_from_filename(path)

    families: set[str] = set()
    try:
        if path.suffix.lower() in {".ttc", ".otc"}:
            collection = TTCollection(str(path), lazy=True)
            try:
                for font in collection.fonts:
                    families.update(_font_family_names(font))
            finally:
                collection.close()
        else:
            font = TTFont(str(path), fontNumber=0, lazy=True)
            try:
                families.update(_font_family_names(font))
            finally:
                font.close()
    except Exception:
        # 损坏字体/权限问题不应让整个扫描失败
        return _alias_from_filename(path)
    return families or _alias_from_filename(path)


def _font_family_names(font: Any) -> set[str]:
    """取单个 face 的 family/typographic family（nameID 1/16）。"""
    out: set[str] = set()
    try:
        records = font["name"].names
    except Exception:
        return out
    for record in records:
        if record.nameID not in (1, 16):
            continue
        try:
            value = record.toUnicode().strip()
        except Exception:
            continue
        if value:
            out.add(value)
    return out


def available_fonts(dirs: Iterable[str | os.PathLike[str]] | None = None) -> set[str]:
    """已安装字体的 family name 集合。

    `dirs` 显式给出时只扫描这些目录（测试注入用）；否则用环境变量 + 平台默认值。
    结果按目录元组缓存在进程内。
    """
    paths = [Path(d) for d in dirs] if dirs is not None else default_font_dirs()
    key = tuple(str(p) for p in paths)
    with _CACHE_LOCK:
        cached = _FONT_CACHE.get(key)
    if cached is not None:
        return set(cached)

    families: set[str] = set()
    for base in paths:
        if not base.is_dir():
            continue
        for root, _subdirs, files in os.walk(base):
            for name in files:
                if Path(name).suffix.lower() not in _FONT_SUFFIXES:
                    continue
                families.update(_families_of(Path(root) / name))

    with _CACHE_LOCK:
        _FONT_CACHE[key] = frozenset(families)
    return set(families)


def clear_font_cache() -> None:
    """清空进程内字体缓存（测试或热装字体后调用）。"""
    with _CACHE_LOCK:
        _FONT_CACHE.clear()


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------


class MissingFontError(RuntimeError):
    """严格模式下首选字体缺失：不静默降级（spec「严格模式失败」）。"""

    def __init__(self, missing_by_role: Mapping[str, str]):
        self.by_role: dict[str, str] = dict(missing_by_role)
        # 去重保序：同一族名可能被多个角色首选
        self.missing: list[str] = list(dict.fromkeys(self.by_role.values()))
        detail = "；".join(f"{role} 需要「{family}」" for role, family in self.by_role.items())
        super().__init__(
            f"严格字体模式：缺少标准公文字体（{detail}）。"
            "请安装字体，或不要设置 FD_SHILUN_FONT_STRICT=1 以按声明顺序回退。"
        )


class FontResolution(BaseModel):
    """一次渲染的字体决策：每个角色选了谁、为什么回退、缺了什么。"""

    chains: dict[str, list[str]] = Field(default_factory=dict)
    chosen: dict[str, str] = Field(default_factory=dict)
    # 每项 {"role", "requested", "chosen", "missing"}，只记录发生回退的角色
    fallbacks: list[dict[str, Any]] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    strict: bool = False
    installed_count: int = 0

    def family_for(self, role: str) -> str:
        try:
            return self.chosen[role]
        except KeyError:
            raise KeyError(
                f"未声明的字体角色：{role!r}（已声明：{', '.join(self.chains)}）"
            ) from None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _strict_from_env() -> bool:
    return os.environ.get(STRICT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_fonts(
    *,
    installed: Iterable[str] | None = None,
    strict: bool | None = None,
    roles: Mapping[str, Sequence[str]] | None = None,
) -> FontResolution:
    """按声明顺序为每个角色选一个可用族名。

    - `installed`：已装 family 集合；None 时扫描本机字体目录（测试可显式注入）。
    - `strict`：None 时读环境变量 FD_SHILUN_FONT_STRICT；严格模式下任一角色
      首选字体不可用即抛 MissingFontError（不静默降级）。
    - `roles`：按需覆盖/追加角色链（外置字体清单的入口），未给出的角色沿用 FONT_ROLES。

    最后一项永远被选作兜底，因此 family_for() 恒为非空字符串。
    """
    installed_set = set(installed) if installed is not None else available_fonts()
    installed_norm = _installed_index(installed_set)
    chains: dict[str, list[str]] = {role: list(chain) for role, chain in FONT_ROLES.items()}
    if roles:
        for role, chain in roles.items():
            seq = [str(f).strip() for f in chain if str(f).strip()]
            if not seq:
                raise ValueError(f"字体角色 {role!r} 的回退链不能为空")
            chains[role] = seq

    strict_mode = _strict_from_env() if strict is None else bool(strict)

    chosen: dict[str, str] = {}
    fallbacks: list[dict[str, Any]] = []
    missing: list[str] = []
    first_missing: dict[str, str] = {}

    for role, chain in chains.items():
        first = chain[0]
        if not family_installed(first, installed_norm):
            first_missing[role] = first
        # 取第一个「已装」的族名；全都没有就用末项兜底（始终非空）
        pick_index = len(chain) - 1
        for index, family in enumerate(chain):
            if family_installed(family, installed_norm):
                pick_index = index
                break
        pick = chain[pick_index]
        chosen[role] = pick
        # 只有选中的不是首选时才记回退；missing 是选中者之前被跳过的族名
        if pick_index > 0:
            fallbacks.append(
                {
                    "role": role,
                    "requested": first,
                    "chosen": pick,
                    "missing": list(chain[:pick_index]),
                }
            )
        for family in chain:
            if not family_installed(family, installed_norm) and family not in missing:
                missing.append(family)

    # `missing` 只报「规范要求的字体」缺哪些：链上的兜底项（如 serif）不是字体，
    # 中间的替代项（如 SimSun 之于宋体）同义于已装的规范字体，报出来只会误导。
    # 每个角色被跳过的具体族名在 fallbacks[].missing 里，逐条可查。
    missing = list(dict.fromkeys(first_missing.values()))

    if strict_mode and first_missing:
        raise MissingFontError(first_missing)

    return FontResolution(
        chains=chains,
        chosen=chosen,
        fallbacks=fallbacks,
        missing=missing,
        strict=strict_mode,
        installed_count=len(installed_set),
    )
