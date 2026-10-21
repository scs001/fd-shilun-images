"""定位契约：跨「引用核验」与「公文验收」共用。

核验与校对都要回答「问题在哪」，且前端要能按位置跳转/高亮。定位有两种粒度：
- 字符区间：正文文本里的 [char_start, char_end)，前端据此切段高亮；
- 结构锚点：公文字段（发文字号/成文日期）或段落序号，字符区间不可得时使用。

两者可同时给出（区间优先）。区间必须有序，避免前端切出错位的高亮。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

LocatorKind = Literal["citation", "paragraph", "char_range", "document", "field"]


class Locator(BaseModel):
    """问题/引用在稿件中的位置。"""

    kind: LocatorKind
    citation_id: str | None = None
    paragraph_index: int | None = Field(default=None, ge=0)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    marker: str | None = Field(default=None, max_length=200)
    field: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _range_ordered(self) -> "Locator":
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end < self.char_start
        ):
            raise ValueError("char_end 不得小于 char_start")
        return self


def citation_locator(
    citation_id: str,
    *,
    char_start: int | None = None,
    char_end: int | None = None,
    marker: str | None = None,
) -> Locator:
    """引用标记的定位（有字符区间就带上，前端可精确定位到标记本身）。"""
    return Locator(
        kind="citation",
        citation_id=citation_id,
        char_start=char_start,
        char_end=char_end,
        marker=marker,
    )


def field_locator(field: str) -> Locator:
    """公文字段级定位（发文字号/成文日期/主送机关……）。"""
    return Locator(kind="field", field=field)


def document_locator() -> Locator:
    """整篇级定位（必需要素缺失这类「无处可指」的问题）。"""
    return Locator(kind="document")
