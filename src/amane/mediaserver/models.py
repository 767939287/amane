"""媒体服务器人物的读模型. ``dto`` 保留服务端原始条目, 回写时整体回传以避免丢字段."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MediaServerPerson(BaseModel):
    id: str
    name: str
    overview: str | None = None
    birth_date: str | None = None
    """``YYYY-MM-DD``; 服务端用 ``PremiereDate`` 存生日."""
    tags: list[str] = Field(default_factory=list)
    provider_ids: dict[str, str] = Field(default_factory=dict)
    dto: dict[str, Any] = Field(default_factory=dict, repr=False)
    """人物列表返回的原始 JSON 对象 (回写时的底稿)."""

    @classmethod
    def from_dto(cls, dto: dict[str, Any]) -> MediaServerPerson | None:
        """缺少 Id / Name 的条目直接丢弃: 服务端偶尔返回空壳."""
        item_id = dto.get("Id")
        name = dto.get("Name")
        if not isinstance(item_id, str) or not item_id:
            return None
        if not isinstance(name, str) or not name.strip():
            return None
        return cls(
            id=item_id,
            name=name.strip(),
            overview=_text(dto.get("Overview")),
            birth_date=_birth_date(dto.get("PremiereDate")),
            tags=[tag for tag in dto.get("Tags") or [] if isinstance(tag, str) and tag],
            provider_ids={str(key): str(value) for key, value in (dto.get("ProviderIds") or {}).items() if value},
            dto=dto,
        )


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _birth_date(value: object) -> str | None:
    """只认 ISO 时间戳的日期部分; 服务端给的其它形状按未填写处理."""
    if not isinstance(value, str):
        return None
    head = value.strip()[:10]
    if len(head) != 10 or head[4] != "-" or head[7] != "-":
        return None
    return head
