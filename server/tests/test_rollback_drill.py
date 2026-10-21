"""enforce 切换与回滚演练测试（task 5.3）。

把演练脚本本身当被测对象：它必须真的证明「切 enforce 有效、回滚后行为恢复」，
而不是只打印 PASS。测试覆盖三个不变量：

1. shadow 与历史行为逐字节一致（回滚目标明确）；
2. enforce 只剥 block，pending 与 pass 不动；
3. 回滚到 shadow 后正文与回滚前完全一致，且开关非法值不放开闸门。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_drill() -> ModuleType:
    """按路径加载演练脚本（避开 site-packages 里的同名 scripts 包）。"""
    path = ROOT / "scripts" / "rollback_drill.py"
    spec = importlib.util.spec_from_file_location("fd_shilun_rollback_drill", path)
    assert spec and spec.loader, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drill = _load_drill()


def test_drill_passes() -> None:
    """整套演练必须返回 0（所有不变量成立）。"""
    assert drill.main([]) == 0


def test_shadow_matches_legacy_behavior() -> None:
    from fd_shilun_api.verify.service import verify_text
    from fd_shilun_api.writing import resolve_citations

    resolver = drill.DrillResolver()
    outcome = verify_text(drill.DRILL_TEXT, drill.DRILL_CITATIONS, mode="shadow", resolver=resolver)
    legacy = resolve_citations(drill.DRILL_TEXT, {c["id"] for c in drill.DRILL_CITATIONS})[
        "cleaned_text"
    ]
    assert outcome.cleaned_text == legacy


def test_enforce_strips_only_blocks() -> None:
    from fd_shilun_api.verify.service import verify_text

    resolver = drill.DrillResolver()
    outcome = verify_text(drill.DRILL_TEXT, drill.DRILL_CITATIONS, mode="enforce", resolver=resolver)
    text = outcome.cleaned_text
    assert "[[cite:c1]]" in text, "可核引用不得被剥"
    assert "[[cite:c2]]" not in text, "撤稿（block）必须被剥"
    assert "[[cite:c3]]" in text, "待核不得被剥"
    assert "[[cite:ghost]]" not in text, "不可解仍按历史行为剥除"
    assert outcome.annotations, "待核必须带标注"


def test_rollback_restores_text_exactly() -> None:
    from fd_shilun_api.verify.service import verify_text

    resolver = drill.DrillResolver()
    before = verify_text(drill.DRILL_TEXT, drill.DRILL_CITATIONS, mode="shadow", resolver=resolver)
    verify_text(drill.DRILL_TEXT, drill.DRILL_CITATIONS, mode="enforce", resolver=resolver)
    after = verify_text(drill.DRILL_TEXT, drill.DRILL_CITATIONS, mode="shadow", resolver=resolver)
    assert after.cleaned_text == before.cleaned_text


def test_env_switch_defaults_to_shadow_and_ignores_bogus(monkeypatch) -> None:
    from fd_shilun_api.verify.service import verification_mode

    monkeypatch.delenv(drill.ENV_KEY, raising=False)
    assert verification_mode() == "shadow"
    monkeypatch.setenv(drill.ENV_KEY, "enforce")
    assert verification_mode() == "enforce"
    monkeypatch.setenv(drill.ENV_KEY, "bogus")
    assert verification_mode() == "shadow", "非法值必须回落 shadow，不得放开闸门"


def test_drill_does_not_leak_env(monkeypatch) -> None:
    """演练跑完不得留下环境变量（否则会污染同进程的其他测试）。"""
    monkeypatch.delenv(drill.ENV_KEY, raising=False)
    drill.main([])
    assert os.environ.get(drill.ENV_KEY) is None
