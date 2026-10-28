from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel
from sqlalchemy import asc, update
from sqlalchemy import delete as sqla_delete
from sqlalchemy.sql.functions import coalesce, count
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.sql.expression import SelectOfScalar

from ..models import SortOrder, Task, TaskLink, TaskSortField, TaskStatus, TaskType
from ..repo_types import _TASK_SORT_COLUMNS, _order_clause, _utcnow
from .base import RepositoryMixinBase, chunks

_ACTIVE_STATUSES = (TaskStatus.QUEUED, TaskStatus.RUNNING)
# 互斥键在 payload 里; 同键已有 queued/running 则复用, 不另建行.
# 其余类型每次新建; 同库串行由 handler 内的锁保证.
_EXCLUSIVE_FIELDS: dict[TaskType, str] = {
    TaskType.ACTOR_SCRAPE: "actor_id",
}


def _scope_tasks[T](
    stmt: SelectOfScalar[T],
    *,
    task_ids: Iterable[int] | None = None,
    statuses: Iterable[TaskStatus] | None = None,
    task_types: Iterable[TaskType] | None = None,
) -> SelectOfScalar[T]:
    if task_ids is not None:
        stmt = stmt.where(col(Task.id).in_(list(task_ids)))
    if statuses:
        stmt = stmt.where(col(Task.status).in_(list(statuses)))
    if task_types:
        stmt = stmt.where(col(Task.type).in_(list(task_types)))
    return stmt


def _payload_dict(payload: dict[str, object] | BaseModel) -> dict[str, object]:
    return payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload


def _exclusive_value(payload: dict[str, object], field: str) -> int | None:
    raw = payload.get(field)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


# 一个入队事务的互斥键索引, 键为 (任务类型, 互斥字段). 由 `_resolve_task` 首次用到时填充.
_ActiveExclusive = dict[tuple[TaskType, str], dict[int, Task]]


async def _preload_exclusive(session: AsyncSession, task_type: TaskType, field: str) -> dict[int, Task]:
    """该类型 queued / running 行的互斥键 -> 行. 每个入队事务只取一次, 逐条入队不再回表."""
    stmt = select(Task).where(col(Task.type) == task_type, col(Task.status).in_(_ACTIVE_STATUSES))
    out: dict[int, Task] = {}
    for task in (await session.exec(stmt)).all():
        value = _exclusive_value(task.payload or {}, field)
        if value is not None:
            out.setdefault(value, task)
    return out


async def _resolve_task(
    session: AsyncSession,
    task_type: TaskType,
    payload: dict[str, object],
    priority: int,
    active: _ActiveExclusive,
) -> Task:
    """有队列互斥命中则返回既有行, 否则新建一条待 flush 的行.

    新建行立即记进 ``active``: 同一事务内后续同键条目复用它, 不必先落库再查回.
    """
    field = _EXCLUSIVE_FIELDS.get(task_type)
    value = _exclusive_value(payload, field) if field is not None else None
    if field is None or value is None:
        task = Task(type=task_type, payload=payload, priority=priority)
        session.add(task)
        return task
    known = active.get((task_type, field))
    if known is None:
        known = active[(task_type, field)] = await _preload_exclusive(session, task_type, field)
    existing = known.get(value)
    if existing is not None:
        return existing
    task = Task(type=task_type, payload=payload, priority=priority)
    session.add(task)
    known[value] = task
    return task


_IS_CHAIN_ROOT = (col(Task.root_task_id).is_(None)) | (col(Task.root_task_id) == col(Task.id))


def _matching_root_ids(
    statuses: Iterable[TaskStatus] | None, task_types: Iterable[TaskType] | None
) -> SelectOfScalar[int]:
    """有 root 用 root, 裸任务用自身 id."""
    stmt = select(coalesce(col(Task.root_task_id), col(Task.id)))
    return _scope_tasks(stmt, statuses=statuses, task_types=task_types).distinct()


async def _types_at_limit(session: AsyncSession, limits: Mapping[TaskType, int]) -> list[TaskType]:
    """已达同时运行上限的类型; ``limits`` 为空时不查库."""
    if not limits:
        return []
    stmt = select(col(Task.type), count()).where(col(Task.status) == TaskStatus.RUNNING).group_by(col(Task.type))
    running: dict[TaskType, int] = {}
    for task_type, n in (await session.exec(stmt)).all():
        key = task_type if isinstance(task_type, TaskType) else TaskType(task_type)
        running[key] = int(n)
    return [task_type for task_type, cap in limits.items() if running.get(task_type, 0) >= cap]


async def _ids_with_external_descendants(session: AsyncSession, ids: Sequence[int]) -> set[int]:
    """待删集合里存在集合外后裔的那些 id (含菱形多父).
    有未纳入本次删除的后裔时祖先须跳过, 否则剩余子任务失去链根后无法在列表中显示.
    """
    id_set = set(ids)
    if not id_set:
        return set()
    children_of: dict[int, list[int]] = {}
    frontier = list(id_set)
    visited = set(id_set)
    while frontier:
        next_frontier: list[int] = []
        # 每层可达上千个节点, 整批放进一条 IN 会超过绑定变量上限.
        for chunk in chunks(frontier):
            rows = await session.exec(
                select(TaskLink.parent_task_id, TaskLink.child_task_id).where(col(TaskLink.parent_task_id).in_(chunk))
            )
            for parent, child in rows.all():
                if parent is None or child is None:
                    continue
                children_of.setdefault(parent, []).append(child)
                if child not in visited:
                    visited.add(child)
                    next_frontier.append(child)
        frontier = next_frontier
    external = visited - id_set
    if not external:
        return set()
    parents_of: dict[int, list[int]] = {}
    for parent, kids in children_of.items():
        for child in kids:
            parents_of.setdefault(child, []).append(parent)
    protected: set[int] = set()
    stack = list(external)
    seen = set(external)
    while stack:
        node = stack.pop()
        for parent in parents_of.get(node, []):
            if parent in id_set:
                protected.add(parent)
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return protected


class TasksRepoMixin(RepositoryMixinBase):
    async def create_task(self, task_type: TaskType, payload: dict[str, object] | BaseModel, priority: int = 0) -> Task:
        tasks = await self.create_tasks(task_type, [payload], priority=priority)
        return tasks[0]

    async def create_tasks(
        self,
        task_type: TaskType,
        payloads: Sequence[dict[str, object] | BaseModel],
        priority: int = 0,
    ) -> list[Task]:
        if not payloads:
            return []
        async with self._task_insert_lock, self._session() as session:
            active: _ActiveExclusive = {}
            tasks = [
                await _resolve_task(session, task_type, _payload_dict(payload), priority, active)
                for payload in payloads
            ]
            # 一次 flush 拿全部主键; 已提交行由 expire_on_commit=False 保持可用, 无需逐条 refresh.
            await session.flush()
            await session.commit()
            return tasks

    async def get_task(self, task_id: int) -> Task | None:
        async with self._session() as session:
            return await session.get(Task, task_id)

    async def claim_next_task(self, *, limits: Mapping[TaskType, int] | None = None) -> Task | None:
        """认领优先级最高的 QUEUED 行, 标为 RUNNING.

        ``limits`` 给出各类型的同时运行上限: 已达上限的类型本次不认领, 其任务保持 QUEUED,
        既不占并发槽也不阻塞其它类型. RUNNING 只由本方法写入, 因此判定与认领落在同一把锁内即可成立.
        """
        async with self._claim_lock, self._session() as session:
            stmt = select(Task).where(Task.status == TaskStatus.QUEUED)
            blocked = await _types_at_limit(session, limits or {})
            if blocked:
                stmt = stmt.where(col(Task.type).notin_(blocked))
            stmt = stmt.order_by(col(Task.priority).desc(), asc(col(Task.created_at))).limit(1)
            result = await session.exec(stmt)
            task = result.first()
            if task is None:
                return None
            task.status = TaskStatus.RUNNING
            task.started_at = _utcnow()
            session.add(task)
            await session.commit()
            await session.refresh(task)
            return task

    async def complete_task(self, task_id: int, result: dict[str, object] | None = None) -> None:
        async with self._session() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.status = TaskStatus.DONE
            task.result = result
            task.finished_at = _utcnow()
            session.add(task)
            await session.commit()

    async def complete_task_with_followups(
        self,
        task_id: int,
        result: dict[str, object] | None,
        followups: Sequence[tuple[str, TaskType, dict[str, object], int]],
    ) -> list[Task]:
        """父完成与子创建须在同一事务. 同父同 key 只留第一条, UNIQUE(parent, key) 拒绝重复."""
        created: list[Task] = []
        async with self._task_insert_lock, self._session() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return created
            if task.status != TaskStatus.RUNNING:
                return created
            task.status = TaskStatus.DONE
            task.result = result
            task.finished_at = _utcnow()
            if task.root_task_id is None:
                task.root_task_id = task.id  # 裸任务成为链根: 指向自己
            session.add(task)
            root_id = task.root_task_id or task.id
            seen_keys: set[str] = set()
            active: _ActiveExclusive = {}
            edges: list[tuple[str, Task]] = []
            for key, task_type, payload, priority in followups:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                child = await _resolve_task(session, task_type, payload, priority, active)
                if child.root_task_id is None:
                    child.root_task_id = root_id
                    session.add(child)
                edges.append((key, child))
                created.append(child)
            # 先一次 flush 拿到全部子任务主键, 再批量写边: 语句数与后继条数线性相关.
            await session.flush()
            links: list[TaskLink] = []
            for key, child in edges:
                if child.id is None:
                    continue
                links.append(TaskLink(parent_task_id=task_id, child_task_id=child.id, key=key))
            session.add_all(links)
            await session.commit()
        return created

    async def fail_task(self, task_id: int, error: str, result: dict[str, object] | None = None) -> None:
        """失败也可带结果载荷: 刮削失败时站点明细仍要能展示; None 会清掉上一次运行的残留."""
        async with self._session() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.status = TaskStatus.FAILED
            task.error = error
            task.result = result
            task.retries += 1
            task.finished_at = _utcnow()
            session.add(task)
            await session.commit()

    async def fail_running_task(self, task_id: int, error: str, result: dict[str, object] | None = None) -> bool:
        """仅当任务仍为 RUNNING 时标记失败; 返回是否命中.

        取消回退与取消兜底使用: 已进入终态的任务不得被覆盖为 FAILED. 取消同样落库载荷.
        """
        async with self._session() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status != TaskStatus.RUNNING:
                return False
            task.status = TaskStatus.FAILED
            task.error = error
            task.result = result
            task.retries += 1
            task.finished_at = _utcnow()
            session.add(task)
            await session.commit()
            return True

    async def fail_all_running_tasks(self) -> int:
        """将全部 RUNNING 标为 FAILED (进程重启后的僵尸任务)."""
        async with self._session() as session:
            stmt = (
                update(Task)
                .where(col(Task.status) == TaskStatus.RUNNING)
                .values(
                    {
                        col(Task.status): TaskStatus.FAILED,
                        col(Task.error): "Task marked as failed due to application restart",
                        col(Task.finished_at): _utcnow(),
                    }
                )
            )
            result = await session.exec(stmt)
            await session.commit()
            return result.rowcount

    async def list_tasks(
        self,
        statuses: Iterable[TaskStatus] | None = None,
        task_types: Iterable[TaskType] | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: TaskSortField = TaskSortField.CREATED_AT,
        order: SortOrder = SortOrder.DESC,
        *,
        roots_only: bool = False,
    ) -> list[Task]:
        async with self._session() as session:
            if roots_only and (statuses or task_types):
                # roots_only 只约束列表显示哪一层: 筛选仍匹配全部任务, 再还原链根.
                stmt = select(Task).where(col(Task.id).in_(_matching_root_ids(statuses, task_types)), _IS_CHAIN_ROOT)
            else:
                stmt = _scope_tasks(select(Task), statuses=statuses, task_types=task_types)
                if roots_only:
                    stmt = stmt.where(_IS_CHAIN_ROOT)
            stmt = (
                stmt.order_by(_order_clause(_TASK_SORT_COLUMNS[sort_by], order), col(Task.id).asc())
                .offset(offset)
                .limit(limit)
            )
            result = await session.exec(stmt)
            return list(result.all())

    async def count_tasks(
        self,
        statuses: Iterable[TaskStatus] | None = None,
        task_types: Iterable[TaskType] | None = None,
        *,
        roots_only: bool = False,
    ) -> int:
        async with self._session() as session:
            if roots_only and (statuses or task_types):
                stmt = select(count()).where(col(Task.id).in_(_matching_root_ids(statuses, task_types)), _IS_CHAIN_ROOT)
            else:
                base = _scope_tasks(select(Task), statuses=statuses, task_types=task_types)
                if roots_only:
                    base = base.where(_IS_CHAIN_ROOT)
                stmt = select(count()).select_from(base.subquery())
            result = await session.exec(stmt)
            return result.one() or 0

    async def list_task_links(
        self, *, parent_task_id: int | None = None, child_task_id: int | None = None
    ) -> list[TaskLink]:
        """可按父或子过滤."""
        async with self._session() as session:
            stmt = select(TaskLink)
            if parent_task_id is not None:
                stmt = stmt.where(col(TaskLink.parent_task_id) == parent_task_id)
            if child_task_id is not None:
                stmt = stmt.where(col(TaskLink.child_task_id) == child_task_id)
            stmt = stmt.order_by(col(TaskLink.id))
            result = await session.exec(stmt)
            return list(result.all())

    async def list_tasks_by_root(self, root_task_id: int) -> list[Task]:
        """同一链全部任务 (含根). root_task_id 在完成事务内写入."""
        async with self._session() as session:
            result = await session.exec(
                select(Task).where(col(Task.root_task_id) == root_task_id).order_by(col(Task.created_at), col(Task.id))
            )
            return list(result.all())

    async def list_children(
        self, parent_task_id: int, *, limit: int | None = None, offset: int = 0
    ) -> list[tuple[Task, str]]:
        """直接子任务, 按边创建顺序, 每项带出边 key."""
        async with self._session() as session:
            stmt = (
                select(Task, col(TaskLink.key))
                .join(TaskLink, col(TaskLink.child_task_id) == col(Task.id))
                .where(col(TaskLink.parent_task_id) == parent_task_id)
                .order_by(col(TaskLink.id))
            )
            if offset:
                stmt = stmt.offset(offset)
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = (await session.exec(stmt)).all()
            return [(task, key) for task, key in rows]

    async def child_status_counts(self, parent_task_ids: Iterable[int]) -> dict[int, dict[TaskStatus, int]]:
        """返回 {parent_id: {status: n}}."""
        ids = list(parent_task_ids)
        if not ids:
            return {}
        async with self._session() as session:
            stmt = (
                select(col(TaskLink.parent_task_id), col(Task.status), count())
                .join(Task, col(Task.id) == col(TaskLink.child_task_id))
                .where(col(TaskLink.parent_task_id).in_(ids))
                .group_by(col(TaskLink.parent_task_id), col(Task.status))
            )
            out: dict[int, dict[TaskStatus, int]] = {}
            for parent_id, status, n in (await session.exec(stmt)).all():
                bucket = out.setdefault(parent_id, {})
                st = status if isinstance(status, TaskStatus) else TaskStatus(status)
                bucket[st] = int(n)
            return out

    async def find_tasks(
        self,
        *,
        task_ids: Iterable[int] | None = None,
        statuses: Iterable[TaskStatus] | None = None,
        task_types: Iterable[TaskType] | None = None,
    ) -> list[Task]:
        """不分页. task_ids 为空可迭代视为无匹配; 大批 id 分批绑定."""
        async with self._session() as session:
            if task_ids is None:
                stmt = _scope_tasks(select(Task), statuses=statuses, task_types=task_types)
                return list((await session.exec(stmt)).all())
            ids = list(task_ids)
            if not ids:
                return []
            found: list[Task] = []
            for chunk in chunks(ids):
                stmt = _scope_tasks(select(Task), task_ids=chunk, statuses=statuses, task_types=task_types)
                found.extend((await session.exec(stmt)).all())
            return found

    async def fail_queued_tasks(
        self,
        *,
        error: str,
        task_ids: Iterable[int] | None = None,
        task_types: Iterable[TaskType] | None = None,
    ) -> int:
        """只标 QUEUED. 返回更新行数."""
        if task_ids is not None:
            ids = list(task_ids)
            if not ids:
                return 0
        else:
            ids = None
        async with self._session() as session:
            stmt = update(Task).where(col(Task.status) == TaskStatus.QUEUED)
            if ids is not None:
                stmt = stmt.where(col(Task.id).in_(ids))
            if task_types:
                stmt = stmt.where(col(Task.type).in_(list(task_types)))
            stmt = stmt.values(
                {
                    col(Task.status): TaskStatus.FAILED,
                    col(Task.error): error,
                    col(Task.finished_at): _utcnow(),
                    col(Task.retries): col(Task.retries) + 1,
                }
            )
            result = await session.exec(stmt)
            await session.commit()
            return result.rowcount

    async def retry_tasks(self, tasks: Sequence[Task]) -> list[Task]:
        """克隆为无根裸任务, 一次提交. 不保留原链归属, 否则会与原有后继冲突."""
        if not tasks:
            return []
        async with self._task_insert_lock, self._session() as session:
            active: _ActiveExclusive = {}
            rows = [
                await _resolve_task(session, task.type, task.payload or {}, task.priority, active) for task in tasks
            ]
            await session.flush()
            created: list[Task] = []
            seen: set[int] = set()
            for row in rows:
                if row.id is None or row.id in seen:
                    continue
                seen.add(row.id)
                created.append(row)
            await session.commit()
            return created

    async def delete_task(self, task_id: int) -> bool:
        """有未纳入本次删除的后裔时拒绝, 否则剩余子任务失去链根后无法在列表中显示."""
        async with self._session() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return False
            if await _ids_with_external_descendants(session, [task_id]):
                return False
            await session.exec(
                sqla_delete(TaskLink).where(
                    (col(TaskLink.parent_task_id) == task_id) | (col(TaskLink.child_task_id) == task_id)
                )
            )
            await session.delete(task)
            await session.commit()
            return True

    async def delete_tasks(self, task_ids: Iterable[int | None]) -> int:
        """含相关后继边. 存在集合外后裔的行跳过. 边与行都分批删除, 一次提交."""
        ids = [i for i in task_ids if i is not None]
        if not ids:
            return 0
        async with self._session() as session:
            protected = await _ids_with_external_descendants(session, ids)
            deletable = [i for i in ids if i not in protected]
            if not deletable:
                return 0
            # 整批任务组可达上万行, 单条语句绑定全部 id 会超过 SQLite 变量上限.
            for chunk in chunks(deletable):
                await session.exec(
                    sqla_delete(TaskLink).where(
                        (col(TaskLink.parent_task_id).in_(chunk)) | (col(TaskLink.child_task_id).in_(chunk))
                    )
                )
            deleted = 0
            for chunk in chunks(deletable):
                result = await session.exec(sqla_delete(Task).where(col(Task.id).in_(chunk)))
                deleted += result.rowcount or 0
            await session.commit()
            return deleted
