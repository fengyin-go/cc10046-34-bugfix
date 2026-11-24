"""任务归属凭据领域服务：把调度计划、批次、来源和领取记录串成可查的凭据链。

设计要点:
- 调度入口每次生成独立任务号，不再用“当前最大行号”冒充任务号；
- 批次靠「任务归属凭据」认亲，而不是只比对批次名或来源名；
- 旧共享任务号允许存在多条来源凭据，拆分完成前只登记领取、不重复派发；
- 补采只读取凭据真正属于原调度的批次，同名批次/同名来源不会串站；
- 领取以请求标识做幂等，响应断开后重提不会多出执行任务。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store

OWNERSHIP_MODULE = "_task_ownership"
CLAIM_MODULE = "_task_claim"
EXECUTION_MODULE = "_task_execution"

LEGACY_TASK_ID = "SCHED-LEGACY-900"
CRED_SOURCE = "来源归属"
CRED_BATCH = "批次归属"
VERIFIED = "已核定"
PENDING = "待补"
BLOCKED = "拆分中"
CLAIMED = "已领取"


class TaskIdentityError(ValueError):
    """任务号、来源、批次和凭据对不上时抛出，由调用层转成显式错误。"""


def _now(seconds: int = 0) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + seconds))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _next_id(module: str) -> int:
    rows = store.rows(module)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _ownership_rows() -> list[dict[str, Any]]:
    return store.rows(OWNERSHIP_MODULE)


def _claim_rows() -> list[dict[str, Any]]:
    return store.rows(CLAIM_MODULE)


def _execution_rows() -> list[dict[str, Any]]:
    return store.rows(EXECUTION_MODULE)


def _schedule_rows() -> list[dict[str, Any]]:
    return store.rows("crawl_schedule")


def find_schedule(task_id: str) -> dict[str, Any] | None:
    for row in _schedule_rows():
        if _text(row.get("任务标识")) == _text(task_id):
            return row
    return None


def _used_task_ids() -> set[str]:
    # 旧共享号永远保留给历史凭据回查，不能被当成新调度号分配出去。
    used: set[str] = {LEGACY_TASK_ID}
    for row in _schedule_rows():
        for field in ("任务标识", "任务号", "原调度任务号"):
            if _text(row.get(field)):
                used.add(_text(row.get(field)))
    for row in _ownership_rows():
        for field in ("任务号", "原调度任务号", "执行任务号"):
            if _text(row.get(field)):
                used.add(_text(row.get(field)))
    for row in _execution_rows():
        for field in ("执行任务号", "原调度任务号"):
            if _text(row.get(field)):
                used.add(_text(row.get(field)))
    return used


def assert_new_task_id(task_id: str) -> str:
    """新调度必须使用独立任务号；旧任务号只允许被历史凭据引用，不能当新调度号。"""
    task_id = _text(task_id)
    if not task_id:
        raise TaskIdentityError("新调度需要独立任务号，任务号不能为空")
    if task_id in _used_task_ids():
        raise TaskIdentityError(f"新调度需要独立任务号，任务号 {task_id} 已被占用")
    return task_id


def allocate_task_id(prefix: str = "SCHED") -> str:
    """在所有已知任务号之外分配一个从未使用过的任务号。"""
    used = _used_task_ids()
    index = 1
    for item in used:
        if item.startswith(f"{prefix}-") and item[len(prefix) + 1:].isdigit():
            index = max(index, int(item[len(prefix) + 1:]) + 1)
    while f"{prefix}-{index}" in used:
        index += 1
    return f"{prefix}-{index}"


def allocate_execution_id() -> str:
    return allocate_task_id("EXEC")


# ---------------------------------------------------------------------------
# 凭据登记与查询
# ---------------------------------------------------------------------------


def _source_credentials(task_id: str, source_site: str | None = None) -> list[dict[str, Any]]:
    rows = [
        row for row in _ownership_rows()
        if _text(row.get("任务号")) == _text(task_id)
        and _text(row.get("凭据类型")) == CRED_SOURCE
    ]
    if source_site is not None:
        rows = [row for row in rows if _text(row.get("来源站点")) == _text(source_site)]
    return rows


def _batch_credentials(task_id: str, source_site: str | None = None) -> list[dict[str, Any]]:
    rows = [
        row for row in _ownership_rows()
        if _text(row.get("任务号")) == _text(task_id)
        and _text(row.get("凭据类型")) == CRED_BATCH
    ]
    if source_site is not None:
        rows = [row for row in rows if _text(row.get("来源站点")) == _text(source_site)]
    return rows


def _credential_by_id(credential_id: str) -> dict[str, Any] | None:
    for row in _ownership_rows():
        if _text(row.get("凭据标识")) == _text(credential_id):
            return row
    return None


def _append_source_credential(
    task_id: str,
    source_site: str,
    *,
    status: str,
    evidence: str,
    operator: str,
    shared: bool = False,
) -> dict[str, Any]:
    row = {
        "id": _next_id(OWNERSHIP_MODULE),
        "凭据标识": f"OWN-{task_id}-{source_site}",
        "任务号": task_id,
        "原调度任务号": task_id,
        "来源站点": source_site,
        "批次标识": "",
        "凭据类型": CRED_SOURCE,
        "凭据状态": status,
        "证据说明": evidence,
        "登记时间": _now(),
        "补证时间": "" if status == PENDING else _now(),
        "操作人": operator,
        "共享任务": bool(shared),
        "重试自": "",
    }
    _ownership_rows().append(row)
    return row


def _append_batch_credential(
    task_id: str,
    source_site: str,
    batch_id: str,
    *,
    status: str = VERIFIED,
    retry_of: str = "",
) -> dict[str, Any]:
    row = {
        "id": _next_id(OWNERSHIP_MODULE),
        "凭据标识": f"BATCH-{task_id}-{source_site}-{batch_id}",
        "任务号": task_id,
        "原调度任务号": task_id,
        "来源站点": source_site,
        "批次标识": batch_id,
        "凭据类型": CRED_BATCH,
        "凭据状态": status,
        "证据说明": f"批次 {batch_id} 归属于 {source_site}",
        "登记时间": _now(),
        "补证时间": "" if status == PENDING else _now(),
        "操作人": "system",
        "共享任务": False,
        "重试自": retry_of,
    }
    _ownership_rows().append(row)
    return row


def _ensure_batch_credential(
    task_id: str, source_site: str, batch_id: str, *, retry_of: str = ""
) -> dict[str, Any]:
    for row in _batch_credentials(task_id, source_site):
        if _text(row.get("批次标识")) == _text(batch_id):
            if retry_of and not _text(row.get("重试自")):
                row["重试自"] = retry_of
            return row
    return _append_batch_credential(
        task_id, source_site, batch_id, status=VERIFIED, retry_of=retry_of)


def ensure_legacy_seed(task_id: str | None = None) -> None:
    """旧共享号按需补齐历史证据，其它任务号不触碰。"""
    if task_id is not None and _text(task_id) != LEGACY_TASK_ID:
        return
    _seed_legacy_rows()


def _seed_legacy_rows() -> None:
    """准备旧共享任务号的历史证据：甲已核定、乙待补，两条历史批次指向同一旧号。

    幂等补齐，只作用于旧共享号本身，不会把历史批次挂到新调度上。
    """
    desired = (
        ("甲站", VERIFIED, "甲站原计划 SCHED-LEGACY-900 覆盖 2026-01 全量，原计划证明已核定",
         "LGC-A-001", "部分成功"),
        ("乙站", PENDING, "乙站补报来源证据迟到，凭据待补",
         "LGC-B-001", "部分成功"),
    )
    for source, status, evidence, batch_id, batch_status in desired:
        if not _source_credentials(LEGACY_TASK_ID, source):
            _append_source_credential(
                LEGACY_TASK_ID, source, status=status, evidence=evidence,
                operator="值班员", shared=True)
        existed = any(
            _text(row.get("批次标识")) == batch_id
            for row in _batch_credentials(LEGACY_TASK_ID, source))
        if not existed:
            _append_batch_credential(LEGACY_TASK_ID, source, batch_id, status=status)

        if any(_text(row.get("批次标识")) == batch_id
               for row in store.rows("batch_tracker")):
            continue
        store.rows("batch_tracker").append({
            "id": _next_id("batch_tracker"),
            "批次标识": batch_id,
            "调度任务": LEGACY_TASK_ID,
            "开始时间": _now(-86400),
            "结束时间": _now(-3600),
            "采集总数": "1200",
            "失败行数": "36",
            "水位标记": "2026-01-31",
            "批次状态": batch_status,
            "status": batch_status,
            "pending": True,
            "abnormal": True,
            "来源站点": source,
            "任务归属凭据": f"BATCH-{LEGACY_TASK_ID}-{source}-{batch_id}",
            "请求标识": "",
            "重试自": "",
        })


def resolve_source(entry: dict[str, Any]) -> str:
    """按实际任务归属凭据定位来源，凭据缺失时才退回行上的来源字段。"""
    credential = _credential_by_id(_text(entry.get("任务归属凭据")))
    if credential is not None:
        return _text(credential.get("来源站点"))
    task_id = _text(entry.get("调度任务"))
    batch_id = _text(entry.get("批次标识"))
    for row in _batch_credentials(task_id):
        if _text(row.get("批次标识")) == batch_id:
            return _text(row.get("来源站点"))
    return _text(entry.get("来源站点"))


def has_ownership(task_id: str, source_site: str) -> bool:
    return bool(_source_credentials(_text(task_id), _text(source_site)))


# ---------------------------------------------------------------------------
# 调度与批次登记
# ---------------------------------------------------------------------------


def domain_create_schedule(
    config_name: str,
    period: str = "daily",
    task_id: str | None = None,
    source_site: str | None = None,
    register_source: bool = True,
) -> dict[str, Any]:
    """登记一条采集调度计划，并确保任务号独立、可回查。"""
    if task_id:
        ensure_legacy_seed(task_id)
    config_name = _text(config_name)
    if not config_name:
        raise TaskIdentityError("关联配置不能为空")
    if task_id:
        task_id = assert_new_task_id(task_id)
    else:
        task_id = allocate_task_id()
    actual_source = _text(source_site)
    entry = {
        "id": _next_id("crawl_schedule"),
        "任务标识": task_id,
        "关联配置": config_name,
        "调度周期": period,
        "起始水位": "2026-01-01",
        "截止水位": time.strftime("%Y-%m-%d"),
        "分片策略": "按来源分片",
        "上次执行": "",
        "下次执行": _now(86400),
        "status": "待启动",
        "pending": True,
        "abnormal": False,
        "来源站点": actual_source,
        "执行任务号": "",
        "任务号来源": "登记" if task_id else "自动分配",
    }
    _schedule_rows().append(entry)
    if register_source and actual_source:
        domain_record_evidence(
            task_id, actual_source,
            evidence=f"{actual_source} 新调度 {task_id} 的来源归属由调度入口登记",
            status=VERIFIED, operator="调度入口")
    return {
        "task_id": task_id,
        "config_name": config_name,
        "period": period,
        "shard_count": 4,
        "start_time": _now(),
        "watermark_start": "2026-01-01",
        "watermark_end": time.strftime("%Y-%m-%d"),
        "source_site": actual_source,
        "schedule_entry": entry,
    }


def domain_track_batch(
    batch_id: str,
    schedule_id: str,
    source_site: str | None = None,
    request_id: str | None = None,
    retry_of: str | None = None,
) -> dict[str, Any]:
    """登记采集批次，并写入实际任务归属凭据。"""
    ensure_legacy_seed(schedule_id)
    batch_id = _text(batch_id)
    schedule_id = _text(schedule_id)
    if not batch_id:
        raise TaskIdentityError("批次标识不能为空")
    if not schedule_id:
        raise TaskIdentityError("调度任务不能为空")

    request_id = _text(request_id)
    if request_id:
        for row in store.rows("batch_tracker"):
            if _text(row.get("请求标识")) == request_id and _text(row.get("调度任务")) == schedule_id:
                row["幂等重放"] = True
                return row

    schedule = find_schedule(schedule_id)
    declared_source = _text(source_site)
    if schedule is not None:
        actual_source = _text(schedule.get("来源站点"))
        if declared_source and actual_source and declared_source != actual_source:
            raise TaskIdentityError(
                f"任务号 {schedule_id} 的实际来源是 {actual_source}，"
                f"与批次声明来源 {declared_source} 不一致")
        if not actual_source and declared_source:
            schedule["来源站点"] = declared_source
            actual_source = declared_source
        if not actual_source:
            actual_source = "未登记来源"
    else:
        actual_source = declared_source or "未登记来源"

    credential = _ensure_batch_credential(schedule_id, actual_source, batch_id)
    if retry_of:
        credential["重试自"] = _text(retry_of)
    if not _source_credentials(schedule_id, actual_source):
        pending_note = "待补" if credential.get("凭据状态") == PENDING else VERIFIED
        _append_source_credential(
            schedule_id, actual_source, status=pending_note,
            evidence=f"批次 {batch_id} 登记时带出的实际来源", operator="批次追踪",
            shared=len(_source_credentials(schedule_id)) > 0)
    now = _now()
    entry = {
        "id": _next_id("batch_tracker"),
        "批次标识": batch_id,
        "调度任务": schedule_id,
        "开始时间": now,
        "结束时间": None,
        "采集总数": "0",
        "失败行数": "0",
        "水位标记": now,
        "批次状态": "执行中",
        "status": "执行中",
        "pending": True,
        "abnormal": False,
        "来源站点": actual_source,
        "任务归属凭据": credential["凭据标识"],
        "请求标识": request_id,
        "重试自": _text(retry_of),
    }
    store.rows("batch_tracker").append(entry)
    return entry


def domain_record_evidence(
    task_id: str,
    source_site: str,
    evidence: str = "",
    status: str = VERIFIED,
    batch_ids: list[str] | None = None,
    operator: str = "值班员",
) -> dict[str, Any]:
    """分别记录每个来源的凭据证据；甲已核定范围和乙待补证据互不覆盖。"""
    ensure_legacy_seed(task_id)
    task_id = _text(task_id)
    source_site = _text(source_site)
    status = _text(status) or VERIFIED
    if not task_id or not source_site:
        raise TaskIdentityError("任务号和来源站点不能为空")
    if status not in (VERIFIED, PENDING):
        raise TaskIdentityError(f"不支持的凭据状态：{status}")

    credentials = _source_credentials(task_id, source_site)
    if credentials:
        credential = credentials[0]
        credential["凭据状态"] = status
        credential["证据说明"] = _text(evidence) or _text(credential.get("证据说明"))
        credential["补证时间"] = "" if status == PENDING else _now()
        credential["操作人"] = operator
        credential["共享任务"] = len(_source_credentials(task_id)) > 1
    else:
        if find_schedule(task_id) is None:
            # 同一旧任务号下的第二个来源，天然是共享任务。
            shared = len(_source_credentials(task_id)) > 0 or task_id == LEGACY_TASK_ID
        else:
            shared = len(_source_credentials(task_id)) > 1
        credential = _append_source_credential(
            task_id, source_site, status=status,
            evidence=_text(evidence), operator=operator, shared=shared)

    provided_batches = [_text(item) for item in (batch_ids or []) if _text(item)]
    if provided_batches:
        for batch_id in provided_batches:
            item = _ensure_batch_credential(task_id, source_site, batch_id)
            item["凭据状态"] = status
    else:
        for item in _batch_credentials(task_id, source_site):
            item["凭据状态"] = status

    # 标记同任务的其他来源为共享来源，保证旧共享号仍可整体查询。
    sources = {_text(row.get("来源站点")) for row in _source_credentials(task_id)}
    if len(sources) > 1:
        for row in _source_credentials(task_id):
            row["共享任务"] = True

    return {
        "ok": True,
        "credential": credential,
        "sources": sorted(sources),
    }


def domain_supplement_evidence(
    task_id: str,
    source_site: str,
    evidence: str = "",
    batch_ids: list[str] | None = None,
    operator: str = "值班员",
) -> dict[str, Any]:
    """补报来源证据：只把该来源的待补凭据转为已核定，不动其他来源。"""
    return domain_record_evidence(
        task_id, source_site, evidence=evidence, status=VERIFIED,
        batch_ids=batch_ids, operator=operator)


def domain_link_batches(task_id: str, links: list[dict[str, Any]]) -> dict[str, Any]:
    """把重复/历史调度批次与执行计划关联成可查的实际任务归属凭据。

    `links` 每项包含 batch_id、source_site，可带 status（已核定/待补）和 retry_of。
    同一条历史批次标识出现多次时按出现顺序分别绑定，不会互相覆盖。
    """
    ensure_legacy_seed(task_id)
    task_id = _text(task_id)
    if not task_id:
        raise TaskIdentityError("任务号不能为空")
    if not links:
        raise TaskIdentityError("至少需要一条批次关联")

    used_rows: set[int] = set()
    for link in links:
        batch_id = _text(link.get("batch_id") or link.get("batch_ids"))
        source = _text(link.get("source_site"))
        if not batch_id or not source:
            raise TaskIdentityError("批次关联必须同时给出批次标识和来源站点")
        status = _text(link.get("status")) or VERIFIED
        if status not in (VERIFIED, PENDING):
            raise TaskIdentityError(f"不支持的凭据状态：{status}")

        evidence_rows = _source_credentials(task_id, source)
        if not evidence_rows:
            domain_record_evidence(
                task_id, source,
                evidence=_text(link.get("evidence")) or f"{source} 历史批次关联补登",
                status=status, batch_ids=[batch_id],
                operator=_text(link.get("operator")) or "值班员")
        else:
            credential = _ensure_batch_credential(
                task_id, source, batch_id,
                retry_of=_text(link.get("retry_of")))
            credential["凭据状态"] = status
            if not _text(evidence_rows[0].get("证据说明")):
                evidence_rows[0]["证据说明"] = _text(link.get("evidence"))

        target = None
        for row in store.rows("batch_tracker"):
            if id(row) in used_rows:
                continue
            if _text(row.get("批次标识")) != batch_id:
                continue
            if _text(row.get("来源站点")) and _text(row.get("来源站点")) != source:
                continue
            target = row
            break
        if target is not None:
            target["调度任务"] = task_id
            target["来源站点"] = source
            credential = _ensure_batch_credential(
                task_id, source, batch_id, retry_of=_text(link.get("retry_of")))
            credential["凭据状态"] = status
            target["任务归属凭据"] = credential["凭据标识"]
            target["重试自"] = _text(link.get("retry_of")) or target.get("重试自") or ""
            used_rows.add(id(target))

    return domain_lookup_task(task_id)


# ---------------------------------------------------------------------------
# 查询与补采
# ---------------------------------------------------------------------------


def _batch_rows_for_source(task_id: str, source_site: str) -> list[dict[str, Any]]:
    """只返回凭据真正属于该调度的批次，同名来源/同名批次不会串站。"""
    credentials = {
        _text(row.get("凭据标识")): row
        for row in _batch_credentials(task_id, source_site)
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in store.rows("batch_tracker"):
        if _text(row.get("调度任务")) != task_id:
            continue
        credential_id = _text(row.get("任务归属凭据"))
        credential = credentials.get(credential_id)
        if credential is None:
            continue
        batch_id = _text(row.get("批次标识"))
        if batch_id in seen:
            continue
        seen.add(batch_id)
        result.append({
            "批次标识": batch_id,
            "来源站点": source_site,
            "任务归属凭据": credential_id,
            "重试自": _text(row.get("重试自")) or _text(credential.get("重试自")),
            "批次状态": _text(row.get("批次状态")) or _text(row.get("status")),
        })
    for credential in credentials.values():
        batch_id = _text(credential.get("批次标识"))
        if batch_id and batch_id not in seen:
            seen.add(batch_id)
            result.append({
                "批次标识": batch_id,
                "来源站点": source_site,
                "任务归属凭据": _text(credential.get("凭据标识")),
                "重试自": _text(credential.get("重试自")),
                "批次状态": _text(credential.get("凭据状态")),
            })
    return result


def domain_batches_for_schedule(
    task_id: str, source_site: str | None = None
) -> list[dict[str, Any]]:
    """补采调度读取批次：按归属凭据过滤，来源为空时返回该调度所有已归属批次。"""
    ensure_legacy_seed(task_id)
    task_id = _text(task_id)
    schedule = find_schedule(task_id)
    if source_site is None and schedule is not None:
        source_site = _text(schedule.get("来源站点")) or None
    if source_site:
        return _batch_rows_for_source(task_id, _text(source_site))
    result: list[dict[str, Any]] = []
    for row in _source_credentials(task_id):
        result.extend(_batch_rows_for_source(task_id, _text(row.get("来源站点"))))
    return result


def _consistency(task_id: str) -> list[str]:
    """核对任务号、计划和实际来源是否对得上，返回不一致说明。"""
    problems: list[str] = []
    schedule = find_schedule(task_id)
    if schedule is None:
        return problems
    declared = _text(schedule.get("来源站点"))
    if not declared:
        return problems
    for row in _source_credentials(task_id):
        site = _text(row.get("来源站点"))
        if site != declared:
            problems.append(
                f"来源凭据 {_text(row.get('凭据标识'))} 指向 {site}，"
                f"与任务号 {task_id} 登记来源 {declared} 不一致")
    for row in store.rows("batch_tracker"):
        if _text(row.get("调度任务")) != task_id:
            continue
        actual = resolve_source(row)
        if actual and actual != declared:
            problems.append(
                f"批次 {_text(row.get('批次标识'))} 的实际来源是 {actual}，"
                f"与任务号 {task_id} 登记来源 {declared} 不一致")
    return problems


def domain_lookup_task(task_id: str, source_site: str | None = None) -> dict[str, Any]:
    """按任务号回查归属凭据、来源证据、批次实际归属和历史领取。"""
    ensure_legacy_seed(task_id)
    task_id = _text(task_id)
    if not task_id:
        raise TaskIdentityError("任务号不能为空")
    schedule = find_schedule(task_id)
    source_rows = _source_credentials(task_id)
    credentials = _batch_credentials(task_id)
    if schedule is None and not source_rows and not credentials:
        raise TaskIdentityError(f"任务号 {task_id} 不存在，也没有可查的归属凭据")

    selected = _text(source_site)
    sources: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        site = _text(row.get("来源站点"))
        if selected and site != selected:
            continue
        group = sources.setdefault(site, {
            "来源站点": site,
            "凭据状态": _text(row.get("凭据状态")),
            "证据说明": _text(row.get("证据说明")),
            "凭据标识": _text(row.get("凭据标识")),
            "补证时间": _text(row.get("补证时间")),
            "操作人": _text(row.get("操作人")),
            "批次": [],
        })
        group["凭据状态"] = _text(row.get("凭据状态"))
        group["证据说明"] = _text(row.get("证据说明"))
        group["凭据标识"] = _text(row.get("凭据标识"))
        group["补证时间"] = _text(row.get("补证时间"))
        group["操作人"] = _text(row.get("操作人"))
    for row in credentials:
        site = _text(row.get("来源站点"))
        if selected and site != selected:
            continue
        group = sources.setdefault(site, {
            "来源站点": site,
            "凭据状态": _text(row.get("凭据状态")),
            "证据说明": "",
            "凭据标识": "",
            "补证时间": "",
            "操作人": "",
            "批次": [],
        })
        group["批次"].append({
            "批次标识": _text(row.get("批次标识")),
            "凭据状态": _text(row.get("凭据状态")),
            "任务归属凭据": _text(row.get("凭据标识")),
            "重试自": _text(row.get("重试自")),
        })

    batches: list[dict[str, Any]] = []
    for site in sources:
        batches.extend(_batch_rows_for_source(task_id, site))

    claim_rows = [
        row for row in _claim_rows()
        if _text(row.get("任务号")) == task_id
        and (not selected or _text(row.get("来源站点")) == selected)
    ]
    claims = [dict(row) for row in claim_rows]
    execution_rows = [
        row for row in _execution_rows()
        if _text(row.get("原调度任务号")) == task_id
        and (not selected or _text(row.get("来源站点")) == selected)
    ]

    source_list = list(sources.values())
    all_source_credentials = _source_credentials(task_id)
    shared = len({_text(row.get("来源站点")) for row in all_source_credentials}) > 1
    split_done = bool(all_source_credentials) and all(
        _text(row.get("凭据状态")) == VERIFIED for row in all_source_credentials)
    pending_sources = sorted({
        _text(row.get("来源站点")) for row in all_source_credentials
        if _text(row.get("凭据状态")) != VERIFIED
    })
    return {
        "ok": True,
        "task_id": task_id,
        "schedule": schedule,
        "declared_source": _text(schedule.get("来源站点")) if schedule else "",
        "shared": shared,
        "split_state": "已完成" if split_done else ("拆分中" if shared else "单来源"),
        "pending_sources": pending_sources,
        "sources": source_list,
        "batches": batches,
        "claims": claims,
        "executions": [dict(row) for row in execution_rows],
        "execution_count": len(execution_rows),
        "inconsistencies": _consistency(task_id),
    }


def _claim_view(claim: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "task_id": _text(claim.get("任务号")),
        "source_site": _text(claim.get("来源站点")),
        "state": _text(claim.get("领取状态")),
        "dispatch": bool(claim.get("已派发")),
        "execution_task_id": _text(claim.get("执行任务号")) or None,
        "batches": list(claim.get("批次") or []),
        "credential_ids": list(claim.get("归属凭据") or []),
        "message": _text(claim.get("说明")),
        "replayed": replayed,
        "request_id": _text(claim.get("请求标识")),
    }


def domain_claim_backfill(
    task_id: str,
    source_site: str,
    request_id: str | None = None,
    origin_batch: str | None = None,
    operator: str = "值班员",
) -> dict[str, Any]:
    """按实际归属凭据领取补采；同一请求重提只回放，不新增执行任务。"""
    ensure_legacy_seed(task_id)
    task_id = _text(task_id)
    source_site = _text(source_site)
    request_id = _text(request_id)
    if not task_id or not source_site:
        raise TaskIdentityError("补采必须指定任务号和来源站点")

    schedule = find_schedule(task_id)
    if schedule is not None:
        problems = _consistency(task_id)
        if problems:
            raise TaskIdentityError(
                f"任务号 {task_id} 与批次实际来源对不上：" + "；".join(problems))
    source_credentials = _source_credentials(task_id, source_site)
    if schedule is None and not source_credentials:
        raise TaskIdentityError(
            f"任务号 {task_id} 没有 {source_site} 的实际归属凭据，不能补采")

    existing_request = None
    if request_id:
        for row in _claim_rows():
            if (_text(row.get("请求标识")) == request_id
                    and _text(row.get("任务号")) == task_id
                    and _text(row.get("来源站点")) == source_site):
                existing_request = row
                break
    if existing_request is not None and _text(existing_request.get("领取状态")) == CLAIMED:
        existing_request["幂等重放"] = True
        return _claim_view(existing_request, replayed=True)

    if not source_credentials:
        raise TaskIdentityError(
            f"任务号 {task_id} 没有 {source_site} 的来源凭据，无法确认实际归属")

    verified = [
        row for row in source_credentials if _text(row.get("凭据状态")) == VERIFIED]
    pending_sources = sorted({
        _text(row.get("来源站点")) for row in _source_credentials(task_id)
        if _text(row.get("凭据状态")) != VERIFIED})

    if not verified:
        message = (
            f"旧共享任务 {task_id} 还在拆分，{source_site} 的来源证据待补，"
            f"本次仍未派发")
        claim = existing_request
        if claim is None:
            claim = {
                "id": _next_id(CLAIM_MODULE),
                "请求标识": request_id or f"CLAIM-{task_id}-{source_site}-{_now()}",
                "任务号": task_id,
                "原调度任务号": task_id,
                "来源站点": source_site,
                "领取状态": BLOCKED,
                "已派发": False,
                "执行任务号": "",
                "批次": [],
                "归属凭据": [row.get("凭据标识") for row in source_credentials],
                "说明": message,
                "领取时间": _now(),
                "操作人": operator,
            }
            _claim_rows().append(claim)
        else:
            claim["领取状态"] = BLOCKED
            claim["已派发"] = False
            claim["说明"] = message
        return _claim_view(claim, replayed=False)

    # 同一来源已经成功领取过，则不重复创建执行任务。
    for row in _claim_rows():
        if (_text(row.get("任务号")) == task_id
                and _text(row.get("来源站点")) == source_site
                and _text(row.get("领取状态")) == CLAIMED):
            row["幂等重放"] = True
            return _claim_view(row, replayed=True)

    batches = _batch_rows_for_source(task_id, source_site)
    batch_ids = [item["批次标识"] for item in batches]
    if not batch_ids:
        message = f"{source_site} 的凭据已核定，但还没有归属到任务 {task_id} 的批次，本次不派发"
        claim = existing_request
        if claim is None:
            claim = {
                "id": _next_id(CLAIM_MODULE),
                "请求标识": request_id or f"CLAIM-{task_id}-{source_site}-{_now()}",
                "任务号": task_id,
                "原调度任务号": task_id,
                "来源站点": source_site,
                "领取状态": BLOCKED,
                "已派发": False,
                "执行任务号": "",
                "批次": [],
                "归属凭据": [row.get("凭据标识") for row in source_credentials],
                "说明": message,
                "领取时间": _now(),
                "操作人": operator,
            }
            _claim_rows().append(claim)
        else:
            claim["领取状态"] = BLOCKED
            claim["已派发"] = False
            claim["说明"] = message
        return _claim_view(claim, replayed=False)

    credential_ids = [
        _text(row.get("凭据标识")) for row in _batch_credentials(task_id, source_site)]
    execution_task_id = allocate_execution_id()
    execution = {
        "id": _next_id(EXECUTION_MODULE),
        "执行任务号": execution_task_id,
        "原调度任务号": task_id,
        "来源站点": source_site,
        "执行类型": "补采",
        "批次": batch_ids,
        "请求标识": request_id,
        "创建时间": _now(),
        "操作人": operator,
    }
    _execution_rows().append(execution)

    pending_note = (
        f"，共享任务仍在拆分（{'、'.join(pending_sources)}待补）" if pending_sources else "")
    message = (
        f"已按 {source_site} 的已核定凭据领取 {len(batch_ids)} 个批次，"
        f"执行任务号 {execution_task_id}{pending_note}")

    claim = existing_request
    if claim is None:
        claim = {
            "id": _next_id(CLAIM_MODULE),
            "请求标识": request_id or f"CLAIM-{task_id}-{source_site}-{execution_task_id}",
            "任务号": task_id,
            "原调度任务号": task_id,
            "来源站点": source_site,
            "领取状态": CLAIMED,
            "已派发": True,
            "执行任务号": execution_task_id,
            "批次": batch_ids,
            "归属凭据": credential_ids,
            "说明": message,
            "领取时间": _now(),
            "操作人": operator,
        }
        _claim_rows().append(claim)
    else:
        claim.update({
            "领取状态": CLAIMED,
            "已派发": True,
            "执行任务号": execution_task_id,
            "批次": batch_ids,
            "归属凭据": credential_ids,
            "说明": message,
            "领取时间": _now(),
        })

    # 重采时留一条历史批次，仍挂同一任务归属凭据，避免下次指错站点。
    origin = _text(origin_batch)
    if origin:
        retry_index = sum(
            1 for row in store.rows("batch_tracker")
            if _text(row.get("重试自")) == origin) + 1
        retry_batch_id = f"{origin}-重试{retry_index}"
        retry_credential = _ensure_batch_credential(
            task_id, source_site, retry_batch_id, retry_of=origin)
        store.rows("batch_tracker").append({
            "id": _next_id("batch_tracker"),
            "批次标识": retry_batch_id,
            "调度任务": task_id,
            "开始时间": _now(),
            "结束时间": "",
            "采集总数": "0",
            "失败行数": "0",
            "水位标记": _now(),
            "批次状态": "执行中",
            "status": "执行中",
            "pending": True,
            "abnormal": False,
            "来源站点": source_site,
            "任务归属凭据": retry_credential["凭据标识"],
            "请求标识": request_id,
            "重试自": origin,
        })

    return _claim_view(claim, replayed=False)
