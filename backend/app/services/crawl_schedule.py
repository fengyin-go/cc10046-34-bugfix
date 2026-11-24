"""采集调度领域服务：生成和登记调度计划，并保证任务标识可回查、不被复用。"""
from __future__ import annotations

from app.services import task_ownership
from app.services.base import BaseModuleService
from app.services.task_ownership import TaskIdentityError


class CrawlScheduleService(BaseModuleService):
    MODULE = "crawl_schedule"
    ENTITY = "调度任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务标识", "关联配置", "调度周期", "起始水位", "截止水位", "分片策略", "上次执行", "下次执行"]
    REQUIRED_FIELDS = ["任务标识", "关联配置", "调度周期"]
    STATUS_ORDER = ["待启动", "运行中", "已完成", "调度失败"]
    ACTION_RULES = {"启动调度": "待启动", "取消调度": "调度失败"}
    NEGATIVE_ACTIONS = []

    def domain_create_schedule(
        self,
        config_name: str,
        period: str = "daily",
        task_id: str | None = None,
        source_site: str | None = None,
    ) -> dict:
        """生成采集调度计划：任务号独立，来源和实际任务号随计划一起登记。"""
        return task_ownership.domain_create_schedule(
            config_name, period, task_id=task_id, source_site=source_site)

    def create_entry(self, values: dict) -> tuple[dict | None, list[str]]:
        """通用登记入口也走任务号校验：缺省自动分配，重复任务号直接报错。"""
        self.last_create_error = ""
        config_name = str(values.get("关联配置") or "").strip()
        period = str(values.get("调度周期") or "daily").strip() or "daily"
        if not config_name:
            return None, ["关联配置"]
        requested = str(values.get("任务标识") or "").strip()
        try:
            plan = self.domain_create_schedule(
                config_name, period, task_id=requested or None,
                source_site=str(values.get("来源站点") or "").strip() or None)
        except TaskIdentityError as error:
            self.last_create_error = str(error)
            return None, []
        entry = plan["schedule_entry"]
        for field in self.LIST_FIELDS:
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        entry["任务标识"] = plan["task_id"]
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict | None, str]:
        """启动调度时把计划里的任务号带到执行任务号，便于批次认亲。"""
        entry, message = super().run_action(entry_id, action)
        if entry is not None and action == "启动调度":
            if not str(entry.get("执行任务号") or "").strip():
                entry["执行任务号"] = task_ownership.allocate_execution_id()
            message = (
                f"{self.ENTITY}已{action}，执行任务号 {entry['执行任务号']}"
                f"（任务号 {entry.get('任务标识')}）")
        return entry, message
