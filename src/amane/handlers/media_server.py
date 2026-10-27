"""按演员拉取服务器人物 (导入) 与回写本地人物资料 (同步)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ..db.models import Actor
from ..enums import OverviewMode
from ..mediaserver import MediaServerClient, MediaServerPerson
from .models import ActorImportPayload, ActorImportResult, ActorSyncPayload, ActorSyncResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from pathlib import Path

    from ..config import HotSettings
    from ..db.repository import Repository
    from ..media import ResourceStore
    from ..net.http import WebClient

logger = structlog.get_logger()


class ActorImportHandler(TaskHandler[ActorImportPayload, ActorImportResult]):
    def __init__(self, repo: Repository, hot: HotSettings, web_client: WebClient) -> None:
        super().__init__(payload_t=ActorImportPayload, result_t=ActorImportResult)
        self._repo = repo
        self._config = hot
        self._web_client = web_client

    async def handle(self, payload: ActorImportPayload) -> TaskResult[ActorImportResult]:
        cfg = self._config.media_server
        if not cfg.enabled:
            return TaskResult(success=False, error="媒体服务器未启用")
        if not cfg.import_missing_actors:
            return TaskResult(success=False, error="已关闭「导入缺失人物」")

        client = MediaServerClient(cfg, self._web_client)
        persons = await client.list_persons()
        logger.info("media server persons fetched", count=len(persons))
        source = str(cfg.server_type)

        created = skipped = failed = 0
        total = len(persons)
        for index, person in enumerate(persons, start=1):
            if await self._repo.lookup_actors_by_name(person.name):
                skipped += 1
            else:
                try:
                    await self._create_actor(person, source)
                except Exception as e:
                    failed += 1
                    logger.warning("actor import failed", name=person.name, error=str(e))
                else:
                    created += 1
            await self.report_progress(index, total, person.name)

        logger.info("actor import completed", created=created, skipped=skipped, failed=failed)
        return TaskResult(
            success=True,
            result=ActorImportResult(total=total, created=created, skipped=skipped, failed=failed),
        )

    async def _create_actor(self, person: MediaServerPerson, source: str) -> None:
        """建档; 同名并发插入时由 ``create_actor`` 返回 None, 计为跳过."""
        await self._repo.create_actor(
            Actor(
                name=person.name,
                birthday=person.birth_date,
                overview=person.overview,
                provider_ids=dict(person.provider_ids),
                field_sources={
                    field: source
                    for field, value in (
                        ("birthday", person.birth_date),
                        ("overview", person.overview),
                        ("provider_ids", person.provider_ids),
                    )
                    if value
                },
            )
        )


class ActorSyncHandler(TaskHandler[ActorSyncPayload, ActorSyncResult]):
    """本地人物资料写回服务器: 简介 / 生日 / 头像 / 标签 / ProviderIds, 写完刷新条目."""

    def __init__(
        self, repo: Repository, hot: HotSettings, web_client: WebClient, resource_store: ResourceStore
    ) -> None:
        super().__init__(payload_t=ActorSyncPayload, result_t=ActorSyncResult)
        self._repo = repo
        self._config = hot
        self._web_client = web_client
        self._resource_store = resource_store

    async def handle(self, payload: ActorSyncPayload) -> TaskResult[ActorSyncResult]:
        cfg = self._config.media_server
        if not cfg.enabled:
            return TaskResult(success=False, error="媒体服务器未启用")

        client = MediaServerClient(cfg, self._web_client)
        by_name = {person.name: person for person in await client.list_persons()}

        synced = skipped = failed = missing = unmatched = 0
        total = len(payload.actor_ids)
        for index, actor_id in enumerate(payload.actor_ids, start=1):
            actor = await self._repo.get_actor(actor_id)
            if actor is None:
                missing += 1
                await self.report_progress(index, total, str(actor_id))
                continue
            person = await self._match_person(actor, by_name)
            if person is None:
                unmatched += 1
            else:
                try:
                    wrote = await self._sync_actor(actor, person, client)
                except Exception as e:
                    failed += 1
                    logger.warning("actor sync failed", actor_id=actor_id, actor_name=actor.name, error=str(e))
                else:
                    if wrote:
                        synced += 1
                    else:
                        skipped += 1
            await self.report_progress(index, total, actor.name)

        logger.info(
            "actor sync completed",
            synced=synced,
            skipped=skipped,
            failed=failed,
            missing=missing,
            unmatched=unmatched,
        )
        return TaskResult(
            success=True,
            result=ActorSyncResult(synced=synced, skipped=skipped, failed=failed, missing=missing, unmatched=unmatched),
        )

    async def _match_person(self, actor: Actor, by_name: dict[str, MediaServerPerson]) -> MediaServerPerson | None:
        """先按展示名, 再按别名行匹配; 服务器人物按名字唯一."""
        names = [actor.name, *await self._repo.get_actor_aliases(actor.id or 0)]
        for name in names:
            person = by_name.get(name)
            if person is not None:
                return person
        return None

    async def _sync_actor(self, actor: Actor, person: MediaServerPerson, client: MediaServerClient) -> bool:
        """写一项返回 True; 无任何可写内容返回 False."""
        cfg = self._config.media_server
        overview = None
        if cfg.sync_overview and actor.overview:
            overview = merge_overview(actor.overview, person.overview, cfg.overview_mode)

        tags = None
        if cfg.sync_tags:
            local_tags = [tag.name for tag in await self._repo.list_actor_user_tags(actor.id or 0)]
            merged_tags = list(dict.fromkeys([*person.tags, *local_tags]))
            if merged_tags != person.tags:
                tags = merged_tags

        provider_ids = None
        if cfg.sync_provider_ids and actor.provider_ids:
            merged_ids = {**person.provider_ids, **actor.provider_ids}
            if merged_ids != person.provider_ids:
                provider_ids = merged_ids

        birth_date = actor.birthday if cfg.sync_birthday else None

        avatar: Path | None = None
        if cfg.sync_avatar and actor.image_urls:
            avatar = await self._resource_store.acquire(actor.image_urls[0], self._web_client)

        if overview is None and tags is None and provider_ids is None and birth_date is None and avatar is None:
            return False

        if overview is not None or tags is not None or provider_ids is not None or birth_date is not None:
            await client.update_person(
                person,
                overview=overview,
                birth_date=birth_date,
                tags=tags,
                provider_ids=provider_ids,
            )
        if avatar is not None:
            await client.upload_primary_image(person.id, avatar)
        await client.refresh_person(person.id)
        return True


def merge_overview(local: str, server: str | None, mode: OverviewMode) -> str:
    """``append_original`` 把服务器原简介当「经历」追加在后.

    重复回写时服务器简介已以本地简介开头, 原样返回, 避免每跑一次叠加一段.
    """
    local = local.strip()
    if mode is OverviewMode.REPLACE or not server:
        return local
    if server.startswith(local):
        return server
    return f"{local}\n\n{server}"
