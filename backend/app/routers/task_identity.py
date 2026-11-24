"""任务归属路由：调度任务号 → 实际来源 → 批次 → 补采领取的完整回查入口。

这些接口把「任务归属凭据」独立出来，批次追踪页和补采调度都从这里取数，
不再靠批次名或来源名猜测归属。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services import task_ownership
from app.services.task_ownership import TaskIdentityError

router = APIRouter(prefix="/api/task-identity", tags=["任务归属"])


def _identity_error(error: TaskIdentityError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(error))


@router.get("/{task_id}")
def lookup_task(
    task_id: str,
    source_site: str | None = Query(default=None),
) -> dict[str, Any]:
    """按任务号回查实际归属：来源证据、批次凭据、历史领取和执行任务。"""
    try:
        return task_ownership.domain_lookup_task(task_id, source_site)
    except TaskIdentityError as error:
        raise _identity_error(error) from error


@router.get("/{task_id}/batches")
def list_schedule_batches(
    task_id: str,
    source_site: str | None = Query(default=None),
) -> dict[str, Any]:
    """补采调度的批次读取口：只返回真正属于原调度、来源一致的批次。"""
    try:
        batches = task_ownership.domain_batches_for_schedule(task_id, source_site)
    except TaskIdentityError as error:
        raise _identity_error(error) from error
    return {
        "task_id": task_id,
        "source_site": source_site,
        "items": batches,
        "total": len(batches),
    }


@router.post("/{task_id}/evidence")
def record_evidence(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """登记或补报某个来源的归属证据；甲已核定和乙待补分别记录。"""
    try:
        result = task_ownership.domain_record_evidence(
            task_id,
            str(payload.get("source_site") or ""),
            evidence=str(payload.get("evidence") or ""),
            status=str(payload.get("status") or task_ownership.VERIFIED),
            batch_ids=payload.get("batch_ids"),
            operator=str(payload.get("operator") or "值班员"),
        )
    except TaskIdentityError as error:
        raise _identity_error(error) from error
    return result


@router.post("/{task_id}/claims")
def claim_backfill(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """按实际任务归属凭据领取补采；同一请求标识重提不会新增执行任务。"""
    try:
        return task_ownership.domain_claim_backfill(
            task_id,
            str(payload.get("source_site") or ""),
            request_id=payload.get("request_id"),
            origin_batch=payload.get("origin_batch"),
            operator=str(payload.get("operator") or "值班员"),
        )
    except TaskIdentityError as error:
        raise _identity_error(error) from error


@router.post("/{task_id}/links")
def link_batches(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把重复调度批次/历史批次关联到任务号，形成可查的实际任务归属凭据。"""
    links = payload.get("links")
    if not isinstance(links, list):
        links = [payload] if payload.get("batch_id") else []
    try:
        return task_ownership.domain_link_batches(task_id, links)
    except TaskIdentityError as error:
        raise _identity_error(error) from error
