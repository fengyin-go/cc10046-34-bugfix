"""批次追踪领域服务：批次、来源和补采都以「任务归属凭据」定位。"""
from __future__ import annotations

from app.services import task_ownership
from app.services.base import BaseModuleService
from app.services.task_ownership import TaskIdentityError


class BatchTrackerService(BaseModuleService):
    MODULE = "batch_tracker"
    ENTITY = "采集批次"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["批次标识", "调度任务", "开始时间", "结束时间", "采集总数", "失败行数", "水位标记", "批次状态"]
    REQUIRED_FIELDS = ["批次标识", "调度任务", "开始时间"]
    STATUS_ORDER = ["执行中", "部分成功", "全部成功", "采集失败"]
    ACTION_RULES = {"重采失败行": "部分成功", "标记完成": "全部成功"}
    NEGATIVE_ACTIONS = []

    def domain_track_batch(
        self,
        batch_id: str,
        schedule_id: str,
        source_site: str | None = None,
        request_id: str | None = None,
        retry_of: str | None = None,
    ) -> dict:
        """追踪采集批次执行状态，同时写入实际任务归属凭据。"""
        return task_ownership.domain_track_batch(
            batch_id, schedule_id, source_site=source_site,
            request_id=request_id, retry_of=retry_of)

    def batch_source(self, entry: dict) -> str:
        """按实际任务归属凭据定位来源，避免把同名来源/同名批次认错站点。"""
        return task_ownership.resolve_source(entry)

    def domain_lookup_batch(self, task_id: str, source_site: str | None = None) -> dict:
        """批次追踪按任务号回查：同名来源和历史重试都按凭据归位。"""
        return task_ownership.domain_lookup_task(task_id, source_site)

    def domain_claim_backfill(
        self,
        task_id: str,
        source_site: str,
        request_id: str | None = None,
        origin_batch: str | None = None,
        operator: str = "值班员",
    ) -> dict:
        """按归属凭据领取补采；响应断开后重提只回放，不再新增执行任务。"""
        return task_ownership.domain_claim_backfill(
            task_id, source_site, request_id=request_id,
            origin_batch=origin_batch, operator=operator)

    def domain_batches_for_schedule(
        self, task_id: str, source_site: str | None = None
    ) -> list[dict]:
        """补采调度读取批次：只认真正属于原调度且来源一致的批次。"""
        return task_ownership.domain_batches_for_schedule(task_id, source_site)

    def list_entries(self, **kwargs):
        items, total = super().list_entries(**kwargs)
        for entry in items:
            resolved = task_ownership.resolve_source(entry)
            if resolved:
                entry["来源站点"] = resolved
        return items, total

    def get_entry(self, entry_id: int):
        entry = super().get_entry(entry_id)
        if entry is not None:
            resolved = task_ownership.resolve_source(entry)
            if resolved:
                entry["来源站点"] = resolved
        return entry

    def run_action(self, entry_id: int, action: str) -> tuple[dict | None, str]:
        """重采失败行按批次的实际任务归属凭据派发，不再按同名来源猜测。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None or action != "重采失败行":
            return entry, message
        task_id = str(entry.get("调度任务") or "").strip()
        source_site = task_ownership.resolve_source(entry)
        try:
            claim = task_ownership.domain_claim_backfill(
                task_id, source_site,
                origin_batch=str(entry.get("批次标识") or "").strip() or None,
                operator="批次追踪")
        except TaskIdentityError as error:
            return entry, str(error)
        entry["来源站点"] = source_site
        entry["重采执行任务号"] = claim.get("execution_task_id") or ""
        if not claim.get("dispatch"):
            return entry, claim.get("message") or message
        return entry, (
            f"{self.ENTITY}已{action}，来源 {source_site}，"
            f"执行任务号 {claim['execution_task_id']}")

    def create_entry(self, values: dict):
        """手工登记批次同样写入任务归属凭据；来源必须能和任务号对上。"""
        batch_id = str(values.get("批次标识") or "").strip()
        schedule_id = str(values.get("调度任务") or "").strip()
        started = str(values.get("开始时间") or "").strip()
        missing = [
            field for field, value in (
                ("批次标识", batch_id), ("调度任务", schedule_id), ("开始时间", started))
            if not value
        ]
        if missing:
            return None, missing
        try:
            entry = task_ownership.domain_track_batch(
                batch_id, schedule_id,
                source_site=str(values.get("来源站点") or "").strip() or None,
                request_id=str(values.get("请求标识") or "").strip() or None,
                retry_of=str(values.get("重试自") or "").strip() or None)
        except TaskIdentityError as error:
            self.last_create_error = str(error)
            return None, []
        entry["开始时间"] = started
        return entry, []
