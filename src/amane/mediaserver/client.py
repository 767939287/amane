"""Emby / Jellyfin 人物接口的最小封装.

两者共用同一套 API 形状, 差异只有根路径 (Emby 带 ``/emby`` 前缀) 与个别枚举大小写;
所有出站请求经 ``WebClient``, 限速与代理沿 ``network`` 配置.
"""

from __future__ import annotations

import asyncio
import mimetypes
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import structlog

from ..enums import MediaServerType
from .models import MediaServerPerson

if TYPE_CHECKING:
    from pathlib import Path

    from ..config import MediaServerConfig
    from ..net.http import HttpMethod, WebClient

logger = structlog.get_logger()

PERSON_PAGE_SIZE = 500
"""人物列表单页条数; 服务器按 TotalRecordCount 决定是否继续翻页."""

_PERSON_FIELDS = "Overview,PremiereDate,Tags,ProviderIds,ImageTags,Taglines"


class MediaServerError(RuntimeError):
    """地址缺失或应答不可解析; 由 handler 转成任务失败."""


class MediaServerClient:
    def __init__(self, config: MediaServerConfig, web_client: WebClient) -> None:
        self._config = config
        self._web_client = web_client

    @property
    def root(self) -> str:
        base = self._config.base_url.strip().rstrip("/")
        if not base:
            raise MediaServerError("未配置媒体服务器地址")
        return f"{base}/emby" if self._config.server_type is MediaServerType.EMBY else base

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"X-Emby-Token": self._config.api_key.strip(), "Accept": "application/json", **extra}

    async def _json(self, method: HttpMethod, url: str) -> Any:
        resp = await self._web_client.request(method, url, headers=self._headers(), timeout=self._config.timeout)
        try:
            return resp.json()
        except ValueError as e:
            raise MediaServerError(f"媒体服务器应答不是 JSON: {url}") from e

    async def list_persons(self) -> list[MediaServerPerson]:
        """拉取全部人物; 分页直到 TotalRecordCount 用尽."""
        persons: list[MediaServerPerson] = []
        start = 0
        while True:
            url = (
                f"{self.root}/Persons?StartIndex={start}&Limit={PERSON_PAGE_SIZE}"
                f"&Fields={_PERSON_FIELDS}&EnableImages=false"
            )
            data = await self._json("GET", url)
            items = data.get("Items") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                break
            for dto in items:
                if not isinstance(dto, dict):
                    continue
                person = MediaServerPerson.from_dto(dto)
                if person is not None:
                    persons.append(person)
            total = data.get("TotalRecordCount")
            start += len(items)
            if not isinstance(total, int) or start >= total:
                break
        return persons

    async def update_person(
        self,
        person: MediaServerPerson,
        *,
        overview: str | None = None,
        birth_date: str | None = None,
        tags: list[str] | None = None,
        provider_ids: dict[str, str] | None = None,
    ) -> None:
        """以服务端原条目为底稿覆盖指定字段后整体回传; 不传的字段保持原样."""
        dto = dict(person.dto)
        dto["Id"] = person.id
        dto["Name"] = person.name
        if overview is not None:
            dto["Overview"] = overview
        if birth_date is not None:
            dto["PremiereDate"] = f"{birth_date}T00:00:00.0000000Z"
        if tags is not None:
            dto["Tags"] = list(tags)
        if provider_ids is not None:
            dto["ProviderIds"] = dict(provider_ids)
        await self._request("POST", f"{self.root}/Items/{quote(person.id, safe='')}", json=dto)

    async def upload_primary_image(self, person_id: str, path: Path) -> None:
        """上传主图; 文件读取放线程, 避免大图阻塞事件循环."""
        payload = await asyncio.to_thread(path.read_bytes)
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        await self._request(
            "POST",
            f"{self.root}/Items/{quote(person_id, safe='')}/Images/Primary",
            data=payload,
            headers={"Content-Type": mime},
        )

    async def refresh_person(self, person_id: str) -> None:
        """写完刷新条目; 不带 metadata / image 刷新模式, 免得提供方把刚写的字段覆盖回去."""
        url = (
            f"{self.root}/Items/{quote(person_id, safe='')}/Refresh"
            "?MetadataRefreshMode=None&ImageRefreshMode=None"
            "&ReplaceAllMetadata=false&ReplaceAllImages=false&Recursive=false"
        )
        await self._request("POST", url)

    async def _request(
        self, method: HttpMethod, url: str, *, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> None:
        await self._web_client.request(
            method,
            url,
            headers={**self._headers(), **(headers or {})},
            timeout=self._config.timeout,
            **kwargs,
        )
