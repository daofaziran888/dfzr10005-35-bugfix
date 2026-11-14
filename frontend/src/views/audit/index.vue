<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>变更审计追踪管理</h2>
        <p class="page-desc">维护数仓变更的操作类型、前后对比与复核状态；归档后只读，回读时逐项核对来源快照。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记变更记录</button>
        <button class="btn" type="button" @click="exportRows">导出变更审计追踪清单</button>
        <button class="btn ghost" type="button" @click="reload">刷新列表</button>
      </div>
    </header>

    <form v-if="creating" class="create-form" @submit.prevent="submitCreate">
      <label v-for="field in createFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="createValues[field]" :placeholder="field" />
      </label>
      <button class="btn primary" type="submit" :disabled="submitting">提交登记</button>
      <button class="btn ghost" type="button" @click="creating = false">取消</button>
    </form>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>复核状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>复核意见（标记复核时必填）</span>
        <input v-model="reviewRemark" placeholder="请填写复核依据与结论" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>来源</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-selected': selectedId === row.id }">
          <td v-for="column in columns" :key="column">
            <span v-if="column === '复核状态'">
              {{ displayStatus(row) }}
              <span v-if="row['只读']" class="readonly-tag">只读</span>
            </span>
            <span v-else-if="column === '变更前值' || column === '变更后值'">{{ row[column] ?? '—' }}</span>
            <span v-else>{{ row[column] ?? '—' }}</span>
            <span v-if="column === '记录编号' && Number(row['修订序号'] ?? 1) > 1" class="revision-tag">
              修订 {{ row['修订序号'] }}
            </span>
          </td>
          <td class="source-cell">
            <button class="link" type="button" @click="openDetail(row)">
              详情 / 回读校验
            </button>
            <span class="source-note">
              {{ row['来源模块'] ?? '手工登记' }} · {{ row['来源记录编号'] ?? '—' }}
            </span>
            <span v-if="(row['缺失项'] as string[] | undefined)?.length" class="missing-text">
              缺失：{{ (row['缺失项'] as string[]).join('、') }}
            </span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actionFor(row)"
              :key="action"
              class="link"
              type="button"
              :disabled="isRunning(row.id, action)"
              @click="runAction(action, row)"
            >
              {{ isRunning(row.id, action) ? '处理中…' : action }}
            </button>
            <span v-if="row['只读']" class="readonly-note">已归档，仅可新建修订</span>
          </td>
        </tr>
        <tr v-if="!rows.length && !loading">
          <td :colspan="columns.length + 2" class="empty-state">暂无变更审计追踪数据，可先登记变更记录</td>
        </tr>
        <tr v-if="loading">
          <td :colspan="columns.length + 2" class="empty-state">正在读取变更审计追踪清单…</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条变更审计追踪记录</span>
      <span v-if="messageText" :class="messageKind === 'error' ? 'error-text' : 'ok-text'">{{ messageText }}</span>
    </footer>

    <section v-if="detail" class="detail-panel" data-panel="audit-detail">
      <header class="detail-head">
        <div>
          <h3>{{ detail['记录编号'] }} · 单条详情</h3>
          <p class="detail-desc">
            {{ detail['变更对象'] }} / {{ detail['变更类型'] }} ·
            复核状态 {{ detail['复核状态'] }}
            <span v-if="detail['只读']" class="readonly-tag">只读</span>
          </p>
        </div>
        <div class="detail-actions">
          <button class="btn ghost" type="button" @click="loadReadback(detail.id as number)">刷新回读校验</button>
          <button class="btn ghost" type="button" @click="closeDetail">关闭详情</button>
        </div>
      </header>

      <div class="detail-grid">
        <div v-for="item in detailFields" :key="item.label" class="detail-item">
          <span class="detail-label">{{ item.label }}</span>
          <strong :class="{ 'missing-text': item.value === null || item.value === undefined || item.value === '' }">
            {{ item.value ?? '缺失（不以当前账号/时间补全）' }}
          </strong>
        </div>
      </div>

      <div class="readback-block">
        <h4>归档回读校验</h4>
        <div v-if="readbackError" class="error-text">{{ readbackError }}</div>
        <div v-else-if="!readback" class="source-empty">点击“刷新回读校验”读取归档快照</div>
        <template v-else>
          <div class="readback-summary">
            <span :class="readback['一致'] ? 'ok-text' : 'error-text'">
              {{ readback['一致'] ? '归档列表 / 单条详情 / 资产来源面板投影一致' : '三处投影不一致，请检查来源事件回读' }}
            </span>
            <span>指纹校验：{{ readback['指纹校验'] }}</span>
            <span v-if="(readback['缺失项'] as string[]).length" class="missing-text">
              缺失项：{{ (readback['缺失项'] as string[]).join('、') }}
            </span>
          </div>
          <table class="data-table">
            <thead>
              <tr>
                <th>核对字段</th>
                <th>归档列表</th>
                <th>单条详情</th>
                <th>资产来源面板</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(values, field) in readbackProjections" :key="String(field)">
                <td>{{ field }}</td>
                <td v-for="source in projectionSources" :key="source" :class="{ 'missing-text': valueOf(values, source) === null }">
                  {{ valueOf(values, source) ?? '缺失' }}
                </td>
              </tr>
            </tbody>
          </table>
        </template>
      </div>

      <div class="readback-block">
        <h4>回读字段逐项核对</h4>
        <table class="data-table">
          <thead>
            <tr><th>字段</th><th>是否存在</th><th>值</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="check in detail['回读校验'] as AuditCheck[]" :key="check['字段']">
              <td>{{ check['标签'] }}</td>
              <td :class="check['存在'] ? 'ok-text' : 'missing-text'">{{ check['存在'] ? '存在' : '缺失' }}</td>
              <td>{{ check['值'] ?? '—' }}</td>
              <td :class="check['存在'] ? '' : 'missing-text'">{{ check['说明'] }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="historyEvents.length" class="readback-block">
        <h4>事件链（归档后只追加修订引用）</h4>
        <table class="data-table">
          <thead>
            <tr><th>记录编号</th><th>修订序号</th><th>变更类型</th><th>前后值</th><th>复核状态</th><th>复核人 / 时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="event in historyEvents" :key="String(event.id)">
              <td>{{ event['记录编号'] }}</td>
              <td>{{ event['修订序号'] }}</td>
              <td>{{ event['变更类型'] }}</td>
              <td>{{ event['变更前值'] ?? '—' }} → {{ event['变更后值'] ?? '—' }}</td>
              <td>{{ event['复核状态'] }}<span v-if="event['只读']" class="readonly-tag">只读</span></td>
              <td>{{ event['复核人'] ?? '缺失' }} / {{ event['复核时间'] ?? '缺失' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
type AuditCheck = Record<string, any>

const ENDPOINT = '/api/audit'
const columns = ['记录编号', '变更对象', '变更类型', '变更前值', '变更后值', '操作人', '操作时间', '复核状态']
const statuses = ['已记录', '已复核', '已归档']
const createFields = ['记录编号', '变更对象', '变更类型', '变更前值', '变更后值', '操作人']

const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const messageText = ref('')
const messageKind = ref<'ok' | 'error'>('ok')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const reviewRemark = ref('')
const filterFields = columns.slice(0, 3)
const selectedId = ref<number | null>(null)
const runningKeys = ref<Set<string>>(new Set())
const creating = ref(false)
const submitting = ref(false)
const createValues = ref<Record<string, string>>({})

const detail = ref<Row | null>(null)
const readback = ref<Row | null>(null)
const readbackError = ref('')
const historyEvents = ref<Row[]>([])
const projectionSources = ['归档列表', '单条详情', '资产来源面板']

const statCards = computed(() => [
  { label: '变更记录总数', value: total.value },
  { label: '已复核记录', value: rows.value.filter((row) => row['复核状态'] === '已复核').length },
  { label: '已归档记录', value: rows.value.filter((row) => row['复核状态'] === '已归档').length },
  { label: '回读缺失项', value: rows.value.filter((row) => (row['缺失项'] as string[] | undefined)?.length).length },
])

const detailFields = computed(() => {
  if (!detail.value) return []
  return [
    { label: '变更对象', value: detail.value['变更对象'] },
    { label: '变更类型', value: detail.value['变更类型'] },
    { label: '变更前值', value: detail.value['变更前值'] },
    { label: '变更后值', value: detail.value['变更后值'] },
    { label: '操作人', value: detail.value['操作人'] },
    { label: '操作人账号快照', value: detail.value['操作人账号快照'] },
    { label: '操作时间', value: detail.value['操作时间'] },
    { label: '原始提交时间', value: detail.value['原始提交时间'] },
    { label: '复核人', value: detail.value['复核人'] },
    { label: '复核时间', value: detail.value['复核时间'] },
    { label: '复核意见', value: detail.value['复核意见'] },
    { label: '复核状态', value: detail.value['复核状态'] },
    { label: '来源模块', value: detail.value['来源模块'] },
    { label: '来源记录编号', value: detail.value['来源记录编号'] },
    { label: '变更对象主键', value: detail.value['变更对象主键'] },
    { label: '归档时间', value: detail.value['归档时间'] },
    { label: '归档指纹', value: detail.value['归档指纹'] },
    { label: '指纹校验', value: detail.value['指纹校验'] },
  ]
})

const readbackProjections = computed<Record<string, Record<string, unknown>>>(() => {
  return (readback.value?.['投影'] as Record<string, Record<string, unknown>>) ?? {}
})

function displayStatus(row: Row) {
  return row['复核状态'] ?? row['status'] ?? '—'
}

function actionFor(row: Row) {
  const available = row['可执行动作']
  if (Array.isArray(available) && available.length) {
    return available as string[]
  }
  const status = displayStatus(row)
  if (status === '已归档') return ['恢复操作']
  if (status === '已复核') return ['归档记录']
  return ['标记复核']
}

function isRunning(id: number, action: string) {
  return runningKeys.value.has(`${id}:${action}`)
}

function valueOf(values: unknown, source: string) {
  return (values as Record<string, unknown> | null)?.[source] ?? null
}

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createValues.value = {}
  creating.value = true
  messageText.value = ''
}

async function submitCreate() {
  submitting.value = true
  messageText.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createValues.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '变更记录登记失败')
    }
    messageKind.value = 'ok'
    messageText.value = payload.message
    creating.value = false
    await reload()
  } catch (error) {
    messageKind.value = 'error'
    messageText.value = error instanceof Error ? error.message : '变更记录登记失败'
  } finally {
    submitting.value = false
  }
}

async function runAction(action: string, row: Row) {
  const key = `${row.id}:${action}`
  if (runningKeys.value.has(key)) return
  runningKeys.value.add(key)
  messageText.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        action,
        // 复核意见由值班人填写；未填时后端会明确拒绝，页面不会用套话替用户补全。
        remark: action === '标记复核' ? reviewRemark.value.trim() || undefined : undefined,
      }),
    })
    const payload = await response.json()
    messageKind.value = payload.ok ? 'ok' : 'error'
    messageText.value = payload.message ?? ''
    if (!payload.ok) {
      // 失败动作必须显式提示，不能静默刷新成“看起来成功”。
      messageKind.value = 'error'
    }
    await reload()
    if (selectedId.value === row.id) {
      await openDetail(row)
    }
  } catch (error) {
    messageKind.value = 'error'
    messageText.value = error instanceof Error ? error.message : '变更审计追踪操作失败'
  } finally {
    runningKeys.value.delete(key)
    runningKeys.value = new Set(runningKeys.value)
  }
}

async function openDetail(row: Row) {
  selectedId.value = Number(row.id)
  messageText.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    detail.value = response.ok ? await response.json() : null
  } catch (error) {
    detail.value = null
    messageKind.value = 'error'
    messageText.value = error instanceof Error ? error.message : '单条详情读取失败'
  }
  await Promise.all([loadReadback(Number(row.id)), loadHistory(Number(row.id))])
}

async function loadReadback(entryId: number) {
  readbackError.value = ''
  readback.value = null
  try {
    const response = await request(`${ENDPOINT}/${entryId}/readback`)
    if (!response.ok) throw new Error('归档回读校验读取失败')
    readback.value = await response.json()
  } catch (error) {
    readbackError.value = error instanceof Error ? error.message : '归档回读校验读取失败'
  }
}

async function loadHistory(entryId: number) {
  historyEvents.value = []
  try {
    const response = await request(`${ENDPOINT}/${entryId}/history`)
    if (!response.ok) throw new Error('事件链读取失败')
    const payload = await response.json()
    historyEvents.value = payload.items ?? []
  } catch (error) {
    historyEvents.value = []
    messageKind.value = 'error'
    messageText.value = error instanceof Error ? error.message : '事件链读取失败'
  }
}

function closeDetail() {
  selectedId.value = null
  detail.value = null
  readback.value = null
  historyEvents.value = []
}

async function reload() {
  messageText.value = ''
  loading.value = true
  const query = new URLSearchParams({ ...filters.value, status: statusFilter.value }).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) throw new Error('变更记录列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    messageKind.value = 'error'
    messageText.value = error instanceof Error ? error.message : '变更审计追踪列表读取失败'
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.create-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.detail-panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; }
.detail-head h3 { margin: 0; font-size: 15px; }
.detail-desc { margin: 4px 0 0; font-size: 12px; color: var(--muted); }
.detail-actions { display: flex; gap: 8px; }
.detail-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px 16px; margin-top: 12px; }
.detail-item { border-top: 1px solid var(--border); padding-top: 6px; }
.detail-label { display: block; font-size: 12px; color: var(--muted); }
.readback-block { margin-top: 16px; }
.readback-block h4 { margin: 0 0 8px; font-size: 14px; }
.readback-summary { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; margin-bottom: 8px; }
.row-selected { background: #f3f7ff; }
.source-cell { font-size: 12px; }
.source-note { display: block; color: var(--muted); }
.readonly-tag { margin-left: 6px; padding: 1px 6px; border-radius: 999px; background: #eef2f7; color: #475569; font-size: 11px; }
.revision-tag { margin-left: 6px; padding: 1px 6px; border-radius: 999px; background: #fff3e0; color: #9a3412; font-size: 11px; }
.readonly-note { font-size: 11px; color: var(--muted); }
.missing-text { color: #b42318; }
.ok-text { color: #067647; }
.source-empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
</style>
