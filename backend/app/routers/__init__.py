"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

任务归属路由必须排在资源路由之前，否则 `/api/{module}/{entry_id}` 会把
`/api/task-identity/{task_id}` 抢先匹配成整型 ID。
"""
from __future__ import annotations

from app.routers import task_identity
from app.routers import resource
from app.routers import chains

ROUTERS = [task_identity, resource, chains]
