<template>
  <section class="ownership" data-testid="task-ownership">
    <header class="ownership-head">
      <div>
        <h3>任务归属凭据</h3>
        <p class="page-desc">
          按任务号回查实际来源、来源证据、批次凭据和历史领取；旧共享号仍可查，新调度必须独立任务号。
        </p>
      </div>
    </header>

    <form class="ownership-form" @submit.prevent="queryTask">
      <label>
        <span>任务号</span>
        <input v-model="taskId" data-testid="ownership-task-id" placeholder="如 SCHED-LEGACY-900" />
      </label>
      <label>
        <span>只看来源（可选）</span>
        <input v-model="sourceFilter" data-testid="ownership-source" placeholder="如 甲站" />
      </label>
      <button class="btn primary" data-testid="ownership-query" type="submit">查询归属</button>
      <button class="btn" data-testid="ownership-link-history" type="button" @click="linkHistory">
        历史两条记录关联旧号
      </button>
      <button class="btn" data-testid="ownership-normal-crawl" type="button" @click="runNormalCrawl">
        平常采集（甲乙各触发一次）
      </button>
    </form>

    <p v-if="message" class="ownership-message" data-testid="ownership-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="ownership-error">{{ errorMessage }}</p>

    <template v-if="result">
      <div class="ownership-summary" data-testid="ownership-summary">
        <span>任务号：<strong>{{ result.task_id }}</strong></span>
        <span data-testid="ownership-split">拆分状态：{{ result.split_state }}</span>
        <span>共享任务：{{ result.shared ? '是' : '否' }}</span>
        <span>待补来源：{{ result.pending_sources.length ? result.pending_sources.join('、') : '无' }}</span>
        <span>执行任务：{{ result.execution_count }} 个</span>
      </div>

      <p v-for="problem in result.inconsistencies" :key="problem" class="error-text">
        {{ problem }}
      </p>

      <table class="data-table">
        <thead>
          <tr>
            <th>来源站点</th>
            <th>来源凭据</th>
            <th>凭据状态</th>
            <th>已归属批次</th>
            <th>证据说明</th>
            <th>补采领取</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="source in result.sources"
            :key="source.来源站点"
            :data-testid="`ownership-source-${source.来源站点}`"
          >
            <td>{{ source.来源站点 }}</td>
            <td>{{ source.凭据标识 || '—' }}</td>
            <td>{{ source.凭据状态 }}</td>
            <td>
              <span
                v-for="batch in source.批次"
                :key="batch.任务归属凭据"
                class="credential-chip"
                :data-testid="`ownership-batch-${source.来源站点}-${batch.批次标识}`"
              >
                {{ batch.批次标识 }}<template v-if="batch.重试自">（重试自 {{ batch.重试自 }}）</template>
              </span>
              <span v-if="!source.批次.length">—</span>
            </td>
            <td>{{ source.证据说明 || '—' }}</td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                :data-testid="`ownership-claim-${source.来源站点}`"
                @click="claim(source.来源站点)"
              >
                补采
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`ownership-evidence-${source.来源站点}`"
                @click="supplement(source.来源站点)"
              >
                补证
              </button>
            </td>
          </tr>
          <tr v-if="!result.sources.length">
            <td colspan="6" class="empty-state">该任务号下还没有来源凭据</td>
          </tr>
        </tbody>
      </table>

      <h4>批次实际归属</h4>
      <table class="data-table">
        <thead>
          <tr><th>批次标识</th><th>实际来源</th><th>任务归属凭据</th><th>重试自</th><th>批次状态</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="batch in result.batches"
            :key="`${batch.批次标识}-${batch.任务归属凭据}`"
            :data-testid="`ownership-batch-row-${batch.批次标识}`"
          >
            <td>{{ batch.批次标识 }}</td>
            <td>{{ batch.来源站点 }}</td>
            <td>{{ batch.任务归属凭据 }}</td>
            <td>{{ batch.重试自 || '—' }}</td>
            <td>{{ batch.批次状态 }}</td>
          </tr>
          <tr v-if="!result.batches.length">
            <td colspan="5" class="empty-state">没有真正属于该调度的批次</td>
          </tr>
        </tbody>
      </table>

      <h4>领取与执行任务</h4>
      <table class="data-table">
        <thead>
          <tr><th>来源站点</th><th>领取状态</th><th>执行任务号</th><th>领取批次</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="(claim, index) in result.claims" :key="`${claim.来源站点}-${index}`">
            <td>{{ claim.来源站点 }}</td>
            <td>{{ claim.领取状态 }}</td>
            <td>{{ claim.执行任务号 || '—' }}</td>
            <td>{{ claim.批次.length ? claim.批次.join('、') : '—' }}</td>
            <td>{{ claim.说明 }}</td>
          </tr>
          <tr v-if="!result.claims.length">
            <td colspan="5" class="empty-state">还没有补采领取记录</td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  claimBackfill,
  linkBatches,
  lookupTask,
  recordEvidence,
  triggerNormalCrawl,
  type TaskLookup,
} from '@/api/task-identity'

const taskId = ref('SCHED-LEGACY-900')
const sourceFilter = ref('')
const result = ref<TaskLookup | null>(null)
const message = ref('')
const errorMessage = ref('')
const requestSeq = ref(0)

async function queryTask() {
  errorMessage.value = ''
  message.value = ''
  if (!taskId.value.trim()) {
    errorMessage.value = '请先填写任务号'
    return
  }
  try {
    result.value = await lookupTask(taskId.value.trim(), sourceFilter.value.trim())
  } catch (error) {
    result.value = null
    errorMessage.value = error instanceof Error ? error.message : '任务归属查询失败'
  }
}

function refresh() {
  return queryTask()
}

async function claim(sourceSite: string) {
  errorMessage.value = ''
  message.value = ''
  requestSeq.value += 1
  const requestId = `UI-${taskId.value}-${sourceSite}-${requestSeq.value}`
  try {
    const payload = await claimBackfill(taskId.value.trim(), {
      source_site: sourceSite,
      request_id: requestId,
    })
    message.value = payload.message
    await refresh()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补采领取失败'
  }
}

async function supplement(sourceSite: string) {
  errorMessage.value = ''
  message.value = ''
  try {
    await recordEvidence(taskId.value.trim(), {
      source_site: sourceSite,
      evidence: `${sourceSite} 补报来源证据`,
      status: '已核定',
    })
    message.value = `${sourceSite} 的来源证据已补报核定`
    await refresh()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补证失败'
  }
}

async function runNormalCrawl() {
  errorMessage.value = ''
  const lines: string[] = []
  try {
    for (const site of ['甲站', '乙站']) {
      const payload = await triggerNormalCrawl({
        config_name: `cfg-${site}`,
        batch_label: `NORMAL-${site}`,
        source_site: site,
      })
      lines.push(`${site} → ${payload.task_id}`)
    }
    message.value = `平常采集任务号：${lines.join('；')}（两站任务号应不同）`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '平常采集触发失败'
  }
}

async function linkHistory() {
  errorMessage.value = ''
  message.value = ''
  try {
    const lookup = await linkBatches(taskId.value.trim(), [
      { batch_id: 'LGC-A-001', source_site: '甲站', status: '已核定' },
      { batch_id: 'LGC-B-001', source_site: '乙站', status: '待补' },
    ])
    result.value = lookup
    message.value = '历史两条记录已指向同一旧任务号，可按来源分别补采'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史批次关联失败'
  }
}
</script>

<style scoped>
.ownership { margin-top: 20px; padding-top: 12px; border-top: 2px solid var(--border); }
.ownership-head h3 { margin: 0; }
.ownership-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 10px 0; }
.ownership-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.ownership-form input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.ownership-summary { display: flex; flex-wrap: wrap; gap: 16px; font-size: 13px; margin: 8px 0; }
.ownership-message { color: #1f6feb; font-size: 13px; }
.credential-chip { display: inline-block; background: #eef4ff; border: 1px solid #c7d7fe; border-radius: 4px; padding: 1px 6px; margin: 1px 4px 1px 0; font-size: 12px; }
.ownership h4 { margin: 16px 0 6px; font-size: 14px; }
</style>
