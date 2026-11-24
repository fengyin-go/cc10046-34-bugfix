import { request } from './client'

export type TaskSource = {
  来源站点: string
  凭据状态: string
  证据说明: string
  凭据标识: string
  补证时间: string
  操作人: string
  批次: {
    批次标识: string
    凭据状态: string
    任务归属凭据: string
    重试自: string
  }[]
}

export type TaskBatch = {
  批次标识: string
  来源站点: string
  任务归属凭据: string
  重试自: string
  批次状态: string
}

export type TaskClaim = {
  领取状态: string
  来源站点: string
  执行任务号: string
  批次: string[]
  说明: string
  幂等重放?: boolean
}

export type TaskExecution = {
  执行任务号: string
  原调度任务号: string
  来源站点: string
  执行类型: string
  批次: string[]
}

export type TaskLookup = {
  ok: boolean
  task_id: string
  schedule: Record<string, unknown> | null
  declared_source: string
  shared: boolean
  split_state: string
  pending_sources: string[]
  sources: TaskSource[]
  batches: TaskBatch[]
  claims: TaskClaim[]
  executions: TaskExecution[]
  execution_count: number
  inconsistencies: string[]
}

export type ClaimResult = {
  ok: boolean
  task_id: string
  source_site: string
  state: string
  dispatch: boolean
  execution_task_id: string | null
  batches: string[]
  credential_ids: string[]
  message: string
  replayed: boolean
  request_id: string
}

async function readJson<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = (payload as { detail?: string }).detail
    throw new Error(detail || `接口返回 ${response.status}`)
  }
  return payload as T
}

export async function lookupTask(taskId: string, sourceSite = ''): Promise<TaskLookup> {
  const query = sourceSite ? `?${new URLSearchParams({ source_site: sourceSite })}` : ''
  return readJson<TaskLookup>(await request(`/api/task-identity/${encodeURIComponent(taskId)}${query}`))
}

export async function claimBackfill(
  taskId: string,
  payload: { source_site: string; request_id?: string; origin_batch?: string },
): Promise<ClaimResult> {
  return readJson<ClaimResult>(await request(
    `/api/task-identity/${encodeURIComponent(taskId)}/claims`,
    { method: 'POST', body: JSON.stringify(payload) },
  ))
}

export async function recordEvidence(
  taskId: string,
  payload: { source_site: string; evidence?: string; status?: string; batch_ids?: string[] },
): Promise<{ ok: boolean; sources: string[] }> {
  return readJson(await request(
    `/api/task-identity/${encodeURIComponent(taskId)}/evidence`,
    { method: 'POST', body: JSON.stringify(payload) },
  ))
}

export async function linkBatches(
  taskId: string,
  links: { batch_id: string; source_site: string; status?: string }[],
): Promise<TaskLookup> {
  return readJson<TaskLookup>(await request(
    `/api/task-identity/${encodeURIComponent(taskId)}/links`,
    { method: 'POST', body: JSON.stringify({ links }) },
  ))
}

export async function triggerNormalCrawl(payload: {
  config_name: string
  batch_label: string
  source_site: string
}): Promise<{ task_id: string; source_site: string }> {
  return readJson(await request('/api/chains/crawl-pipeline', {
    method: 'POST',
    body: JSON.stringify(payload),
  }))
}
