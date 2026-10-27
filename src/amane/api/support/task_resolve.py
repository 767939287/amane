"""提交请求 → 任务 payload: 库内任务由 library_id 派生 path, REFRESH / SCAN_INVALID 另派生 recursive / patterns."""

from typing import TYPE_CHECKING, assert_never

from ...db.models import TaskType
from ...handlers import (
    ActorImportPayload,
    ActorScrapePayload,
    ActorSyncPayload,
    CleanupPayload,
    DeletePayload,
    OrganizePayload,
    R18ImportPayload,
    RefreshPayload,
    RescrapePayload,
    ScanInvalidPayload,
    ScrapePayload,
    UpscalePayload,
)
from ..models import (
    ActorImportSubmission,
    ActorScrapeSubmission,
    ActorSyncSubmission,
    CleanupSubmission,
    DeleteSubmission,
    OrganizeSubmission,
    R18ImportSubmission,
    RefreshSubmission,
    RescrapeSubmission,
    ScanInvalidSubmission,
    ScrapeSubmission,
    TaskSubmission,
    UpscaleSubmission,
)

if TYPE_CHECKING:
    from ...db.repository import Repository

ResolvedPayload = (
    RefreshPayload
    | ScrapePayload
    | ScanInvalidPayload
    | DeletePayload
    | OrganizePayload
    | CleanupPayload
    | UpscalePayload
    | R18ImportPayload
    | ActorScrapePayload
    | ActorImportPayload
    | ActorSyncPayload
    | RescrapePayload
)


async def resolve_submission(req: TaskSubmission, repo: Repository) -> tuple[TaskType, ResolvedPayload]:
    match req:
        case RefreshSubmission():
            await req.resolve(repo)
            return TaskType.REFRESH, req
        case OrganizeSubmission():
            await req.resolve(repo)
            return TaskType.ORGANIZE, req
        case DeleteSubmission():
            return TaskType.DELETE, req
        case ScanInvalidSubmission():
            await req.resolve(repo)
            return TaskType.SCAN_INVALID, req
        case ScrapeSubmission():
            return TaskType.SCRAPE, await req.resolve(repo)
        case CleanupSubmission():
            return TaskType.CLEANUP, CleanupPayload(
                remove_missing_files=req.remove_missing_files,
                remove_unreferenced_resources=req.remove_unreferenced_resources,
            )
        case UpscaleSubmission():
            return TaskType.UPSCALE, UpscalePayload(
                max_dim_threshold=req.max_dim_threshold, max_bytes_threshold=req.max_bytes_threshold, limit=req.limit
            )
        case R18ImportSubmission():
            return TaskType.R18_IMPORT, R18ImportPayload(force=req.force)
        case ActorScrapeSubmission():
            return TaskType.ACTOR_SCRAPE, ActorScrapePayload(actor_id=req.actor_id, use_cache=req.use_cache)
        case ActorImportSubmission():
            return TaskType.ACTOR_IMPORT, ActorImportPayload()
        case ActorSyncSubmission():
            return TaskType.ACTOR_SYNC, ActorSyncPayload(actor_ids=req.actor_ids)
        case RescrapeSubmission():
            return TaskType.RESCRAPE, RescrapePayload(
                limit=req.limit, min_age_days=req.min_age_days, targets=req.targets
            )
        case _:
            assert_never(req)
