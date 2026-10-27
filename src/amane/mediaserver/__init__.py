"""Emby / Jellyfin 人物接口. 契约见 ``docs/dev/media-server.md``."""

from .client import MediaServerClient, MediaServerError
from .models import MediaServerPerson

__all__ = ["MediaServerClient", "MediaServerError", "MediaServerPerson"]
