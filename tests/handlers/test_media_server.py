"""ACTOR_IMPORT / ACTOR_SYNC handler 单元测试: 只导入缺失、回写字段与计数."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from amane.config import HotSettings, MediaServerConfig
from amane.db.models import Actor
from amane.enums import MediaServerType, OverviewMode
from amane.handlers.media_server import ActorImportHandler, ActorSyncHandler
from amane.handlers.models import ActorImportPayload, ActorSyncPayload
from amane.mediaserver import MediaServerClient, MediaServerPerson

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.media import ResourceStore
    from amane.net.http import WebClient


def _person(name: str, **overrides: object) -> MediaServerPerson:
    dto = {"Id": name, "Name": name, **overrides}
    person = MediaServerPerson.from_dto(dto)
    assert person is not None
    return person


def _hot(
    *,
    import_missing_actors: bool = True,
    sync_avatar: bool = True,
    overview_mode: OverviewMode = OverviewMode.APPEND_ORIGINAL,
) -> HotSettings:
    return HotSettings(
        media_server=MediaServerConfig(
            enabled=True,
            server_type=MediaServerType.JELLYFIN,
            base_url="http://nas:8096",
            api_key="secret",
            import_missing_actors=import_missing_actors,
            sync_avatar=sync_avatar,
            overview_mode=overview_mode,
        )
    )


def _web() -> WebClient:
    return cast("WebClient", AsyncMock())


@pytest.mark.asyncio(loop_scope="function")
async def test_import_creates_only_missing(repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地已有同名人物 (含别名) 跳过, 只给缺失的建档."""
    existing = await repo.create_actor(Actor(name="Alice"))
    assert existing is not None
    monkeypatch.setattr(
        MediaServerClient,
        "list_persons",
        AsyncMock(
            return_value=[
                _person("Alice", Overview="服务器简介"),
                _person(
                    "Bob", Overview="新简介", PremiereDate="1990-01-02T00:00:00.0000000Z", ProviderIds={"Tmdb": "7"}
                ),
            ]
        ),
    )

    result = await ActorImportHandler(repo, _hot(), _web()).handle(ActorImportPayload())

    assert result.success
    assert result.result is not None
    assert (result.result.total, result.result.created, result.result.skipped) == (2, 1, 1)
    actors = await repo.list_actors(limit=10)
    names = {a.name for a in actors}
    assert names == {"Alice", "Bob"}
    bob = next(a for a in actors if a.name == "Bob")
    assert bob.birthday == "1990-01-02"
    assert bob.overview == "新简介"
    assert bob.provider_ids == {"Tmdb": "7"}
    assert bob.field_sources == {"birthday": "jellyfin", "overview": "jellyfin", "provider_ids": "jellyfin"}


@pytest.mark.asyncio(loop_scope="function")
async def test_import_rejects_when_disabled(repo: Repository) -> None:
    result = await ActorImportHandler(repo, HotSettings(), _web()).handle(ActorImportPayload())

    assert not result.success
    assert result.error == "媒体服务器未启用"


@pytest.mark.asyncio(loop_scope="function")
async def test_import_rejects_when_missing_import_off(repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    """开关关闭时不联网, 直接失败."""
    list_persons = AsyncMock(return_value=[])
    monkeypatch.setattr(MediaServerClient, "list_persons", list_persons)

    result = await ActorImportHandler(repo, _hot(import_missing_actors=False), _web()).handle(ActorImportPayload())

    assert not result.success
    assert result.error == "已关闭「导入缺失人物」"
    list_persons.assert_not_awaited()


@pytest.mark.asyncio(loop_scope="function")
async def test_sync_writes_merged_fields(
    repo: Repository,
    resource_store: ResourceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """简介按 append 模式拼接, 标签与外部 ID 取并集, 写完刷新条目."""
    created = await repo.create_actor(
        Actor(
            name="Alice",
            overview="本地简介",
            birthday="1990-01-02",
            provider_ids={"Tmdb": "42"},
            image_urls=[],
        )
    )
    assert created is not None and created.id is not None
    user_tags, _ = await repo.ensure_user_tags(["本地标签"])
    tag_id = user_tags[0].id
    assert tag_id is not None
    await repo.apply_actor_user_tags([created.id], [tag_id], action="attach")
    monkeypatch.setattr(
        MediaServerClient,
        "list_persons",
        AsyncMock(return_value=[_person("Alice", Overview="服务器原简介", Tags=["server"], ProviderIds={"Tmdb": "1"})]),
    )
    update_person = AsyncMock()
    refresh_person = AsyncMock()
    monkeypatch.setattr(MediaServerClient, "update_person", update_person)
    monkeypatch.setattr(MediaServerClient, "refresh_person", refresh_person)

    result = await ActorSyncHandler(repo, _hot(), _web(), resource_store).handle(
        ActorSyncPayload(actor_ids=[created.id])
    )

    assert result.success
    assert result.result is not None
    assert (result.result.synced, result.result.unmatched, result.result.failed) == (1, 0, 0)
    assert update_person.await_args is not None
    kwargs = update_person.await_args.kwargs
    assert kwargs["overview"] == "本地简介\n\n服务器原简介"
    assert kwargs["birth_date"] == "1990-01-02"
    assert kwargs["tags"] == ["server", "本地标签"]
    assert kwargs["provider_ids"] == {"Tmdb": "42"}
    refresh_person.assert_awaited_once()


@pytest.mark.asyncio(loop_scope="function")
async def test_sync_counts_missing_and_unmatched(
    repo: Repository,
    resource_store: ResourceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await repo.create_actor(Actor(name="Alice", overview="本地简介"))
    assert created is not None and created.id is not None
    monkeypatch.setattr(MediaServerClient, "list_persons", AsyncMock(return_value=[_person("Bob")]))
    update_person = AsyncMock()
    monkeypatch.setattr(MediaServerClient, "update_person", update_person)

    result = await ActorSyncHandler(repo, _hot(), _web(), resource_store).handle(
        ActorSyncPayload(actor_ids=[created.id, 999])
    )

    assert result.result is not None
    assert (result.result.missing, result.result.unmatched, result.result.synced) == (1, 1, 0)
    update_person.assert_not_awaited()


@pytest.mark.asyncio(loop_scope="function")
async def test_sync_skips_empty_actor(
    repo: Repository,
    resource_store: ResourceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地无简介 / 生日 / 头像 / 标签时不上传也不刷新."""
    created = await repo.create_actor(Actor(name="Alice"))
    assert created is not None and created.id is not None
    monkeypatch.setattr(MediaServerClient, "list_persons", AsyncMock(return_value=[_person("Alice")]))
    update_person = AsyncMock()
    refresh_person = AsyncMock()
    monkeypatch.setattr(MediaServerClient, "update_person", update_person)
    monkeypatch.setattr(MediaServerClient, "refresh_person", refresh_person)

    result = await ActorSyncHandler(repo, _hot(), _web(), resource_store).handle(
        ActorSyncPayload(actor_ids=[created.id])
    )

    assert result.result is not None
    assert (result.result.skipped, result.result.synced) == (1, 0)
    update_person.assert_not_awaited()
    refresh_person.assert_not_awaited()


@pytest.mark.asyncio(loop_scope="function")
async def test_sync_failure_does_not_abort_batch(
    repo: Repository,
    resource_store: ResourceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await repo.create_actor(Actor(name="Alice", overview="A"))
    second = await repo.create_actor(Actor(name="Bob", overview="B"))
    assert first is not None and first.id is not None
    assert second is not None and second.id is not None
    monkeypatch.setattr(
        MediaServerClient,
        "list_persons",
        AsyncMock(return_value=[_person("Alice"), _person("Bob")]),
    )
    monkeypatch.setattr(
        MediaServerClient,
        "update_person",
        AsyncMock(side_effect=[RuntimeError("boom"), None]),
    )
    monkeypatch.setattr(MediaServerClient, "refresh_person", AsyncMock())

    result = await ActorSyncHandler(repo, _hot(overview_mode=OverviewMode.REPLACE), _web(), resource_store).handle(
        ActorSyncPayload(actor_ids=[first.id, second.id])
    )

    assert result.success
    assert result.result is not None
    assert (result.result.failed, result.result.synced) == (1, 1)


@pytest.mark.asyncio(loop_scope="function")
async def test_sync_upload_only_when_avatar_enabled(
    repo: Repository,
    resource_store: ResourceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭头像回写时不下载也不上传, 其余字段照写."""
    created = await repo.create_actor(Actor(name="Alice", overview="本地", image_urls=["https://img/a.jpg"]))
    assert created is not None and created.id is not None
    monkeypatch.setattr(MediaServerClient, "list_persons", AsyncMock(return_value=[_person("Alice")]))
    monkeypatch.setattr(MediaServerClient, "update_person", AsyncMock())
    monkeypatch.setattr(MediaServerClient, "refresh_person", AsyncMock())
    upload = AsyncMock()
    monkeypatch.setattr(MediaServerClient, "upload_primary_image", upload)

    result = await ActorSyncHandler(repo, _hot(sync_avatar=False), _web(), resource_store).handle(
        ActorSyncPayload(actor_ids=[created.id])
    )

    assert result.result is not None
    assert result.result.synced == 1
    upload.assert_not_awaited()
