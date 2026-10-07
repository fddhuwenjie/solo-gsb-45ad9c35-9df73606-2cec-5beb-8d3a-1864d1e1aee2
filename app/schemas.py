"""请求 / 响应的 Pydantic 模型。"""

from __future__ import annotations

from typing_extensions import Self

from pydantic import BaseModel, Field, model_validator

# 段级状态：
#   missing   —— 尚无任何译文
#   submitted —— 存在依据当前原文版本、待确认的译文
#   confirmed —— 存在依据当前原文版本的已确认译文
#   stale     —— 最新译文依据的是旧原文版本（已确认译文失效，待复核）
SEGMENT_MISSING = "missing"
SEGMENT_SUBMITTED = "submitted"
SEGMENT_CONFIRMED = "confirmed"
SEGMENT_STALE = "stale"


class CreateManuscript(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    segments: list[str] = Field(min_length=1)


class RewriteSource(BaseModel):
    text: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)


class SubmitTranslation(BaseModel):
    text: str = Field(min_length=1)
    source_version: int = Field(ge=1)


class ConfirmTranslation(BaseModel):
    source_version: int = Field(ge=1)


class PublishRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class ReorderItem(BaseModel):
    """编排清单项：二选一。

    * source_position —— 引用"调整前"的旧段位置（旧段不能被重复引用）；
    * source_text     —— 在该位置插入一段全新原文（新版本从 1 起，无译文）。
    """

    source_position: int | None = Field(default=None, ge=0)
    source_text: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _exactly_one_reference(self) -> Self:
        if (self.source_position is None) == (self.source_text is None):
            raise ValueError("每一项必须且只能提供 source_position 或 source_text 之一")
        return self


class ReorderSegments(BaseModel):
    expected_revision: int = Field(ge=1)
    # 清单至少保留一段；进一步要求至少引用一段旧段（不能整篇全删再全新建）
    segments: list[ReorderItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _at_least_one_retained(self) -> Self:
        if all(item.source_position is None for item in self.segments):
            raise ValueError("编排清单必须至少保留一段调整前的旧段")
        return self



class TranslationView(BaseModel):
    source_version: int
    target_text: str
    status: str
    updated_at: str


class SegmentView(BaseModel):
    position: int
    source_text: str
    source_version: int
    status: str
    translation: TranslationView | None = None
    # 失效待复核的旧译文（若有），随工作稿一并展示供复核参考
    stale_translation: TranslationView | None = None


class DraftView(BaseModel):
    manuscript_id: int
    title: str
    revision: int
    segments: list[SegmentView]


class PublicationSegment(BaseModel):
    position: int
    source_version: int
    source_text: str
    target_text: str


class PublicationView(BaseModel):
    edition: int
    revision: int
    title: str
    published_at: str
    segments: list[PublicationSegment]
