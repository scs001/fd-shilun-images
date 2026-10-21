"""引用核验（tasks 1.1-1.5）：一级可解 / 二级真实 / 三级支持，三态判定。

模块：
- models      结果模型与报告汇总（三态、定位、依据）
- resolvable  一级「可解」：引用能否解析到本项目收藏条目
- authentic   二级「真实」：元数据源逐字段比对 + 撤稿标记
- support     三级「支持」：被引原句与数字是否真在原文里
- service     三级编排、shadow/enforce 模式与文本处置
"""

from .models import (
    REPORT_VERSION,
    Evidence,
    Status,
    VerificationCounts,
    VerificationItem,
    VerificationReport,
    build_report,
)

__all__ = [
    "REPORT_VERSION",
    "Evidence",
    "Status",
    "VerificationCounts",
    "VerificationItem",
    "VerificationReport",
    "build_report",
]
