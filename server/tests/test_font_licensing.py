"""字体授权守卫测试（deploy/COMPLIANCE.md §2）。

不变量（spec gongwen-typesetting「字体授权与降级」）：
- 系统 SHALL NOT 在未取得授权的情况下随产品分发商业字体文件。

这条不变量不能靠约定，要靠测试守。本测试扫描仓库，一旦出现商业中文字体
二进制就失败——防止有人「顺手」把 simsun.ttc 或方正字体提交进来。

同时验证：代码只读字体元数据、不复制字体文件；回退链末项始终有兜底族名。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fd_shilun_api.gongwen import fonts

REPO_ROOT = Path(__file__).resolve().parents[2]

FONT_SUFFIXES = {".ttf", ".ttc", ".otf", ".otc", ".woff", ".woff2"}

# 商业/受限中文字体：文件名特征（大小写不敏感）。
# 说明：Windows 自带的 simsun/simhei/simkai/simfang 与方正（FZ*）字体
# 可以「使用」但不能「分发」，因此仓库里出现它们即为授权风险。
COMMERCIAL_PATTERNS = (
    r"^simsun", r"^simhei", r"^simkai", r"^simfang", r"^simli", r"^simyou",
    r"^fz",                     # 方正全系（FZXBSJW / FZFSJW …）
    r"^st[a-z]*(song|kai|fang|hei)",  # 华文 STSong/STKaiti/STFangsong/STHeiti
    r"^msyh", r"^msjh",         # 微软雅黑 / 正黑
    r"^pingfang",               # 苹方
    r"^sourcehan", r"^notosanscjk", r"^notoserifcjk",  # 思源/Noto CJK 的「CJK」版
)

# 允许随产品分发的开源字体：SIL OFL / Apache-2.0
PERMISSIVE_PATTERNS = (
    r"^notoserifsc",  # Noto Serif SC（OFL-1.1，web 端已在用）
    r"^notosanssc",
    r"^lxgw",         # 霞鹜文楷（OFL）
    r"^sourcehansans", r"^sourcehanserif",  # 思源黑/宋（OFL，非 CJK 合版）
)


def _iter_font_files() -> list[Path]:
    """仓库内的字体二进制（跳过依赖目录与构建产物缓存）。"""
    skip = {"node_modules", ".git", "__pycache__", ".venv", "dist"}
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in FONT_SUFFIXES:
            continue
        if any(part in skip for part in path.parts):
            continue
        out.append(path)
    return out


def _basename_key(path: Path) -> str:
    """取文件名主干并小写，用于模式匹配（如 noto-serif-sc-90-600-normal.woff2）。"""
    name = path.stem.lower()
    # vite 产物带哈希后缀，统一去掉：noto-serif-sc-90-600-normal-E1rh_Ede
    name = re.sub(r"-[a-z0-9_]{6,}$", "", name)
    return name


@pytest.mark.skipif(
    not REPO_ROOT.exists(), reason="仓库根不可达（在打包环境里运行）"
)
def test_repository_ships_no_commercial_font_binaries() -> None:
    """仓库不得包含商业字体二进制。"""
    offenders: list[str] = []
    for path in _iter_font_files():
        key = _basename_key(path)
        if any(re.match(p, key) for p in COMMERCIAL_PATTERNS):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        "仓库出现商业字体二进制，违反 spec「字体授权与降级」：\n  "
        + "\n  ".join(offenders)
        + "\n处理：从仓库移除，改为在部署镜像里由客户自备或另行授权。"
    )


def test_bundled_fonts_are_permissively_licensed() -> None:
    """仓库内若有字体，必须命中宽松许可白名单。"""
    unknown: list[str] = []
    for path in _iter_font_files():
        key = _basename_key(path)
        if any(re.match(p, key) for p in PERMISSIVE_PATTERNS):
            continue
        if any(re.match(p, key) for p in COMMERCIAL_PATTERNS):
            continue  # 由上一条测试负责报错
        unknown.append(str(path.relative_to(REPO_ROOT)))
    assert unknown == [], (
        "以下字体文件不在宽松许可白名单内，需先确认授权再入库：\n  "
        + "\n  ".join(unknown)
    )


def test_font_module_never_copies_font_files() -> None:
    """字体模块只读元数据：不得出现复制/写入字体文件的调用。"""
    source = (REPO_ROOT / "server" / "fd_shilun_api" / "gongwen" / "fonts.py").read_text(
        encoding="utf-8"
    )
    forbidden = ("shutil.copy", "shutil.copytree", "open(", "write_bytes", "write_text")
    hits = [token for token in forbidden if token in source]
    # open( 允许出现在只读场景；这里只禁止写字体文件相关调用
    hits = [h for h in hits if h in ("shutil.copy", "shutil.copytree", "write_bytes", "write_text")]
    assert hits == [], f"fonts.py 出现写文件调用，可能复制字体：{hits}"


def test_every_role_fallback_chain_ends_with_fallback_family() -> None:
    """每条回退链末项必须是通用兜底族名（不会写出空字体名）。"""
    generic = {"宋体", "serif", "sans-serif", "SimSun"}
    for role, chain in fonts.FONT_ROLES.items():
        assert chain, f"角色 {role} 的回退链为空"
        assert chain[-1] in generic, (
            f"角色 {role} 的回退链末项 {chain[-1]!r} 不是通用兜底族名 {generic}；"
            "缺字体环境会写出不可解析的字体名"
        )


def test_strict_mode_fails_without_standard_fonts() -> None:
    """严格模式：缺首选字体必须失败，不静默降级。"""
    with pytest.raises(fonts.MissingFontError):
        fonts.resolve_fonts(installed={"完全不存在的字体"}, strict=True)


def test_non_strict_mode_falls_back_and_reports() -> None:
    """非严格模式：回退并在报告里列明缺失与替代。"""
    resolution = fonts.resolve_fonts(installed={"宋体"}, strict=False)
    assert resolution.fallbacks, "缺标准字体时必须记录回退"
    assert resolution.missing, "缺标准字体时必须列出缺失字体名"
    for item in resolution.fallbacks:
        assert item.get("requested"), "回退记录必须说明「要求哪个字体」"
        assert item.get("chosen"), "回退记录必须说明「实际用了哪个字体」"
        assert item.get("role"), "回退记录必须说明是哪个排版角色"
