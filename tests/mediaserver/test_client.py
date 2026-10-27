"""媒体服务器人物接口: 根路径、分页、回写底稿与图片上传."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from amane.config import MediaServerConfig
from amane.enums import MediaServerType, OverviewMode
from amane.handlers.media_server import merge_overview
from amane.mediaserver import MediaServerClient, MediaServerError, MediaServerPerson

if TYPE_CHECKING:
    from pathlib import Path

    from amane.net.http import WebClient


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeWebClient:
    """按调用顺序返回预置应答; 没有更多应答时给空对象."""

    def __init__(self, payloads: list[Any] | None = None) -> None:
        self._payloads = list(payloads or [])
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((method, url, kwargs))
        payload = self._payloads.pop(0) if self._payloads else {}
        return _FakeResponse(payload)


def _config(
    server_type: MediaServerType = MediaServerType.EMBY, base_url: str = "http://nas:8096/"
) -> MediaServerConfig:
    return MediaServerConfig(enabled=True, server_type=server_type, base_url=base_url, api_key="secret")


def _client(web: _FakeWebClient, config: MediaServerConfig | None = None) -> MediaServerClient:
    return MediaServerClient(config or _config(), cast("WebClient", web))


@pytest.mark.parametrize(
    ("server_type", "expected_root"),
    [
        (MediaServerType.EMBY, "http://nas:8096/emby"),
        (MediaServerType.JELLYFIN, "http://nas:8096"),
    ],
)
def test_root_follows_server_type(server_type: MediaServerType, expected_root: str) -> None:
    assert _client(_FakeWebClient(), _config(server_type)).root == expected_root


def test_root_requires_base_url() -> None:
    """地址为空时报错, 而不是拼出无效 URL 交给 HTTP 层."""
    with pytest.raises(MediaServerError, match="未配置媒体服务器地址"):
        assert _client(_FakeWebClient(), MediaServerConfig()).root


@pytest.mark.asyncio(loop_scope="function")
async def test_list_persons_pages_and_parses() -> None:
    web = _FakeWebClient(
        [
            {
                "Items": [
                    {
                        "Id": "1",
                        "Name": " Alice ",
                        "Overview": " bio ",
                        "PremiereDate": "1990-01-01T00:00:00.0000000Z",
                        "Tags": ["tag", ""],
                        "ProviderIds": {"Tmdb": "11", "Empty": ""},
                    }
                ],
                "TotalRecordCount": 3,
            },
            {"Items": [{"Id": "2", "Name": "Bob"}, {"Name": "NoId"}], "TotalRecordCount": 3},
        ]
    )

    persons = await _client(web).list_persons()

    assert [p.name for p in persons] == ["Alice", "Bob"]
    assert persons[0].birth_date == "1990-01-01"
    assert persons[0].overview == "bio"
    assert persons[0].tags == ["tag"]
    assert persons[0].provider_ids == {"Tmdb": "11"}
    assert web.calls[0][2]["headers"]["X-Emby-Token"] == "secret"
    assert "StartIndex=0" in web.calls[0][1]
    assert "StartIndex=1" in web.calls[1][1]


@pytest.mark.asyncio(loop_scope="function")
async def test_list_persons_stops_on_empty_page() -> None:
    web = _FakeWebClient([{"Items": [], "TotalRecordCount": 10}])

    assert await _client(web).list_persons() == []
    assert len(web.calls) == 1


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("server_type", [MediaServerType.EMBY, MediaServerType.JELLYFIN])
async def test_authorization_header_carries_token(server_type: MediaServerType) -> None:
    """Jellyfin 12 起默认关闭旧式鉴权, 只认 ``Authorization`` 里的令牌; 旧头留给老版本."""
    web = _FakeWebClient()

    await _client(web, _config(server_type)).refresh_person("9")

    headers = web.calls[0][2]["headers"]
    assert headers["X-Emby-Token"] == "secret"
    assert headers["Authorization"].startswith('MediaBrowser Client="Amane"')
    assert 'Token="secret"' in headers["Authorization"]


@pytest.mark.asyncio(loop_scope="function")
async def test_device_id_follows_base_url() -> None:
    """设备标识随地址固定: 同一台服务器反复请求不新增设备记录, 换地址才是另一台."""
    first, repeat, other = _FakeWebClient(), _FakeWebClient(), _FakeWebClient()
    await _client(first).refresh_person("1")
    await _client(repeat).refresh_person("1")
    await _client(other, _config(base_url="http://nas:8097/")).refresh_person("1")

    def device_id(web: _FakeWebClient) -> str:
        auth = web.calls[0][2]["headers"]["Authorization"]
        return auth.split('DeviceId="', 1)[1].split('"', 1)[0]

    assert device_id(first) == device_id(repeat)
    assert device_id(first) != device_id(other)


@pytest.mark.asyncio(loop_scope="function")
async def test_update_person_keeps_server_fields() -> None:
    """以服务端原条目为底稿, 只覆盖调用方给出的字段."""
    dto = {
        "Id": "1",
        "Name": "Alice",
        "Overview": "原简介",
        "PremiereDate": "1980-01-01T00:00:00.0000000Z",
        "Tags": ["server"],
        "ImageTags": {"Primary": "abc"},
    }
    person = MediaServerPerson.from_dto(dto)
    assert person is not None
    web = _FakeWebClient()

    await _client(web).update_person(
        person,
        overview="本地简介",
        birth_date="1990-05-06",
        tags=["server", "local"],
        provider_ids={"Tmdb": "42"},
    )

    method, url, kwargs = web.calls[0]
    assert (method, url) == ("POST", "http://nas:8096/emby/Items/1")
    assert kwargs["json"] == {
        "Id": "1",
        "Name": "Alice",
        "Overview": "本地简介",
        "PremiereDate": "1990-05-06T00:00:00.0000000Z",
        "Tags": ["server", "local"],
        "ImageTags": {"Primary": "abc"},
        "ProviderIds": {"Tmdb": "42"},
    }


@pytest.mark.asyncio(loop_scope="function")
async def test_update_person_omits_unset_fields() -> None:
    person = MediaServerPerson.from_dto({"Id": "1", "Name": "Alice", "Overview": "原简介"})
    assert person is not None
    web = _FakeWebClient()

    await _client(web).update_person(person, birth_date="1990-01-02")

    sent = web.calls[0][2]["json"]
    assert sent["PremiereDate"].startswith("1990-01-02")
    assert sent["Overview"] == "原简介"
    assert "Tags" not in sent


@pytest.mark.asyncio(loop_scope="function")
async def test_upload_primary_image_uses_file_mime(tmp_path: Path) -> None:
    image = tmp_path / "avatar.png"
    image.write_bytes(b"png-bytes")
    web = _FakeWebClient()

    await _client(web).upload_primary_image("7", image)

    method, url, kwargs = web.calls[0]
    assert (method, url) == ("POST", "http://nas:8096/emby/Items/7/Images/Primary")
    assert kwargs["data"] == b"png-bytes"
    assert kwargs["headers"]["Content-Type"] == "image/png"


@pytest.mark.asyncio(loop_scope="function")
async def test_refresh_person_disables_metadata_refresh() -> None:
    web = _FakeWebClient()

    await _client(web).refresh_person("9")

    method, url, _ = web.calls[0]
    assert method == "POST"
    assert url.startswith("http://nas:8096/emby/Items/9/Refresh?")
    assert "MetadataRefreshMode=None" in url
    assert "ImageRefreshMode=None" in url


@pytest.mark.parametrize(
    ("premiere", "expected"),
    [
        ("1990-01-01T00:00:00.0000000Z", "1990-01-01"),
        ("1990-01-01", "1990-01-01"),
        ("", None),
        (None, None),
        ("unknown", None),
        (1990, None),
    ],
)
def test_person_birth_date_parsing(premiere: object, expected: str | None) -> None:
    person = MediaServerPerson.from_dto({"Id": "1", "Name": "Alice", "PremiereDate": premiere})
    assert person is not None
    assert person.birth_date == expected


def test_merge_overview_modes() -> None:
    assert merge_overview("本地", "原", OverviewMode.REPLACE) == "本地"
    assert merge_overview("本地", "原", OverviewMode.APPEND_ORIGINAL) == "本地\n\n原"
    assert merge_overview("本地", None, OverviewMode.APPEND_ORIGINAL) == "本地"
    # 重复回写: 服务器简介已含本地简介时不叠加.
    assert merge_overview("本地", "本地\n\n原", OverviewMode.APPEND_ORIGINAL) == "本地\n\n原"
