import asyncio
from typing import TYPE_CHECKING

from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from . import repos

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class Repository(
    repos.AgentRepoMixin,
    repos.FacetsRepoMixin,
    repos.FeedsRepoMixin,
    repos.LibrariesRepoMixin,
    repos.MediaRepoMixin,
    repos.MetadataRepoMixin,
    repos.SchedulesRepoMixin,
    repos.TasksRepoMixin,
):
    def __init__(self, engine: AsyncEngine):
        self._engine = engine
        # SQLite DEFERRED: 两个 session 的 SELECT 都在写锁前看到空表. 入队互斥检查与 INSERT 同锁.
        self._task_insert_lock = asyncio.Lock()
        # 认领与并发上限判定同锁: 否则多个并发认领会同时读到"无运行中任务", 一起越过上限.
        self._claim_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """``create_all`` 仅补缺表; schema 演进须经 Alembic."""
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    def _session(self) -> AsyncSession:
        return AsyncSession(self._engine, expire_on_commit=False)
