# 网络数据采集与RPA自动化平台

面向网页数据采集、自动化流程编排、解析规则管理、代理池调度与采集结果校验的数据采集工程平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8010/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5174/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8010`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 爬虫配置 | `crawler_config` | 爬虫配置 | 配置标识、起始地址、页面编码 |
| 解析规则 | `parser_rule` | 解析规则 | 规则标识、目标页面、字段选择器 |
| 代理池 | `proxy_pool` | 代理节点 | 节点标识、代理地址、代理协议 |
| Cookie管理 | `cookie_mgr` | Cookie会话 | 会话标识、来源站点、Cookie内容 |
| 限速控制 | `rate_limiter` | 限速策略 | 策略标识、目标站点、请求频率 |
| 抽取模板 | `extraction_template` | 抽取模板 | 模板标识、适用站点、列表选择器 |
| 数据校验器 | `data_validator` | 校验规则 | 规则标识、校验字段、校验类型 |
| 采集调度 | `crawl_schedule` | 调度任务 | 任务标识、关联配置、调度周期 |
| 结果存储 | `result_store` | 存储批次 | 批次标识、来源配置、采集时间 |
| 去重引擎 | `dedup_engine` | 去重策略 | 策略标识、业务键列、排序字段 |
| 字段映射 | `field_mapper` | 映射方案 | 方案标识、来源字段、目标字段 |
| 异常重试 | `error_retry` | 重试策略 | 策略标识、错误类型、重试次数 |
| 批次追踪 | `batch_tracker` | 采集批次 | 批次标识、调度任务、开始时间 |
| 导出通道 | `export_channel` | 导出任务 | 任务标识、来源批次、导出格式 |
| 采集审计 | `crawl_audit` | 审计记录 | 记录标识、操作类型、操作对象 |
| 来源认证 | `source_auth` | 认证凭证 | 凭证标识、来源站点、认证方式 |
| HTML清洗 | `html_cleaner` | 清洗模板 | 模板标识、适用站点、标签过滤 |
| 增量同步 | `incremental_sync` | 同步水位 | 水位标识、来源配置、增量字段 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 任务归属凭据（采集调度 / 批次追踪）

调度和批次不再靠「批次名 + 来源名」互相认亲，而是由
`backend/app/services/task_ownership.py` 维护一条可查的凭据链：

- 调度入口每次分配独立任务号（`SCHED-n`），重复使用已被占用的任务号会显式报错；
- 每个批次登记时写入「任务归属凭据」，来源以凭据为准，批次行上的来源字段只是冗余展示；
- 旧共享任务号 `SCHED-LEGACY-900` 可以同时挂多份来源凭据：甲站「已核定」、乙站「待补」，
  两条历史批次都指向同一旧号，仍能按旧号整体查询；
- 补采只读取凭据真正属于原调度、且来源一致的批次，同名批次/同名来源不会串站；
- 来源证据未核定时只登记领取、不派发执行任务；补证后再领才会生成执行任务号；
- 领取按请求标识幂等，响应断开后原样重提不会多出执行任务；同一来源重复领取也只回放；
- 批次追踪的「重采失败行」按凭据来源派发，并留下仍挂同一凭据的历史重试批次；
- 新调度任务号与批次实际来源对不上时，接口返回 409/失败说明，查询结果里也会列出 `inconsistencies`。

相关接口：

```text
GET  /api/task-identity/{task_id}                 # 凭据、来源证据、批次、领取、执行任务
GET  /api/task-identity/{task_id}/batches         # 补采调度读取批次（可按 source_site 过滤）
POST /api/task-identity/{task_id}/evidence        # 登记/补报某个来源的归属证据
POST /api/task-identity/{task_id}/claims          # 按归属凭据领取补采（request_id 幂等）
```

批次追踪页面底部有「任务归属凭据」面板，可直接按任务号回查、分别补采甲乙来源、
补证，并触发两站平常采集来核对任务号是否独立。

领域：数据工程
子方向：数据采集、爬虫与RPA
