<template>
  <section class="page" data-module="catalog">
    <header class="page-head">
      <div>
        <h2>数据资产盘点管理</h2>
        <p class="page-desc">
          维护数据资产的分类归属、业务域映射与等级评估；资产动作与生命周期动作共用审计事件，
          同一对象并发写入只接受一个版本。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记资产记录</button>
        <button class="btn" type="button" @click="exportRows">导出数据资产盘点清单</button>
        <button class="btn ghost" type="button" @click="loadGovernance">核对资产治理清单</button>
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
        <span>资产状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>来源事件</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-selected': selectedId === row.id }">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="source-cell">
            <button class="link" type="button" @click="openDetail(row)">
              {{ row['来源事件编号'] ?? '尚无来源事件' }}
            </button>
            <span v-if="row['只读']" class="readonly-tag">只读</span>
            <span v-if="row['版本'] !== undefined" class="source-note">对象版本 v{{ row['版本'] }}</span>
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
            <span v-if="row['只读']" class="readonly-note">已归档，不再接受业务动作</span>
          </td>
        </tr>
        <tr v-if="!rows.length && !loading">
          <td :colspan="columns.length + 2" class="empty-state">暂无数据资产盘点数据，可先登记资产记录</td>
        </tr>
        <tr v-if="loading">
          <td :colspan="columns.length + 2" class="empty-state">正在读取资产记录…</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据资产盘点记录</span>
      <span v-if="messageText" :class="messageKind === 'error' ? 'error-text' : 'ok-text'">{{ messageText }}</span>
    </footer>

    <SourceEventsPanel v-if="selectedId !== null" endpoint="/api/catalog" :entry-id="selectedId" />

    <section v-if="governance" class="detail-panel" data-panel="catalog-governance">
      <header class="detail-head">
        <div>
          <h3>资产治理清单</h3>
          <p class="detail-desc">
            共 {{ governance['total'] }} 条来源事件：已记录 {{ governance['recorded'] }}、
            已复核 {{ governance['reviewed'] }}、已归档 {{ governance['archived'] }}。
          </p>
        </div>
        <button class="btn ghost" type="button" @click="governance = null">收起清单</button>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>记录编号</th><th>变更对象</th><th>变更类型</th><th>变更前值</th><th>变更后值</th>
            <th>复核状态</th><th>操作人</th><th>操作时间</th><th>指纹校验</th><th>缺失项</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in governance['items'] as Row[]" :key="String(item.id)">
            <td>{{ item['记录编号'] }}</td>
            <td>{{ item['变更对象'] }}</td>
            <td>{{ item['变更类型'] }}</td>
            <td>{{ item['变更前值'] ?? '—' }}</td>
            <td>{{ item['变更后值'] ?? '—' }}</td>
            <td>{{ item['复核状态'] }}<span v-if="item['只读']" class="readonly-tag">只读</span></td>
            <td>{{ item['操作人'] }}</td>
            <td>{{ item['操作时间'] }}</td>
            <td :class="item['指纹校验'] === '指纹一致' ? 'ok-text' : ''">{{ item['指纹校验'] }}</td>
            <td :class="(item['缺失项'] as string[] | undefined)?.length ? 'missing-text' : ''">
              {{ (item['缺失项'] as string[] | undefined)?.join('、') || '—' }}
            </td>
          </tr>
          <tr v-if="!(governance['items'] as Row[]).length">
            <td colspan="10" class="empty-state">该模块暂无审计事件</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import SourceEventsPanel from '@/components/SourceEventsPanel.vue'
import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/catalog'
const columns = ['资产编号', '表名称', '资产分类', '业务域', '负责人', '数据等级', '更新频率', '盘点时间']
const statuses = ['草稿', '已盘点', '已复核', '已归档']
const createFields = ['资产编号', '表名称', '资产分类']
const ACTIONS: Record<string, { from: string[]; to: string }> = {
  提交复核: { from: ['草稿', '已盘点'], to: '已盘点' },
  归档资产: { from: ['已盘点', '已复核'], to: '已复核' },
  废弃资产: { from: ['已复核'], to: '已归档' },
}

const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const messageText = ref('')
const messageKind = ref<'ok' | 'error'>('ok')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const filterFields = columns.slice(0, 3)
const selectedId = ref<number | null>(null)
const runningKeys = ref<Set<string>>(new Set())
const creating = ref(false)
const submitting = ref(false)
const createValues = ref<Record<string, string>>({})
const governance = ref<Row | null>(null)

const statCards = computed(() => [
  { label: '资产记录总数', value: total.value },
  { label: '已复核资产', value: rows.value.filter((row) => row['status'] === '已复核').length },
  { label: '已归档资产', value: rows.value.filter((row) => row['只读']).length },
  { label: '关联来源事件', value: rows.value.filter((row) => !!row['来源事件编号']).length },
])

function actionFor(row: Row) {
  if (row['只读']) return []
  const current = String(row['status'] ?? '')
  return Object.entries(ACTIONS)
    .filter(([, rule]) => rule.from.includes(current) && rule.to !== current)
    .map(([action]) => action)
}

function isRunning(id: number, action: string) {
  return runningKeys.value.has(`${id}:${action}`)
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
    if (!response.ok || !payload.ok) throw new Error(payload.message ?? '资产记录登记失败')
    messageKind.value = 'ok'
    messageText.value = payload.message
    creating.value = false
    await reload()
  } catch (error) {
    messageKind.value = 'error'
    messageText.value = error instanceof Error ? error.message : '资产记录登记失败'
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
      body: JSON.stringify({ action, expected_version: row['版本'] }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '数据资产盘点动作未生效')
    }
    messageKind.value = 'ok'
    messageText.value = payload.message ?? ''
    await reload()
  } catch (error) {
    messageKind.value = 'error'
    messageText.value = error instanceof Error ? error.message : '数据资产盘点操作失败'
    await reload()
  } finally {
    runningKeys.value.delete(key)
    runningKeys.value = new Set(runningKeys.value)
  }
}

function openDetail(row: Row) {
  selectedId.value = Number(row.id)
}

async function loadGovernance() {
  try {
    const response = await request(`${ENDPOINT}/governance`)
    if (!response.ok) throw new Error('资产治理清单读取失败')
    governance.value = await response.json()
  } catch (error) {
    messageKind.value = 'error'
    messageText.value = error instanceof Error ? error.message : '资产治理清单读取失败'
  }
}

async function reload() {
  messageText.value = ''
  loading.value = true
  const query = new URLSearchParams({ ...filters.value, status: statusFilter.value }).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) throw new Error('资产记录列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    messageKind.value = 'error'
    messageText.value = error instanceof Error ? error.message : '数据资产盘点列表读取失败'
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.create-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.detail-panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }
.detail-head h3 { margin: 0; font-size: 15px; }
.detail-desc { margin: 4px 0 0; font-size: 12px; color: var(--muted); }
.row-selected { background: #f3f7ff; }
.source-cell { font-size: 12px; }
.source-note { display: block; color: var(--muted); }
.readonly-tag { margin-left: 6px; padding: 1px 6px; border-radius: 999px; background: #eef2f7; color: #475569; font-size: 11px; }
.readonly-note { font-size: 11px; color: var(--muted); }
.missing-text { color: #b42318; }
.ok-text { color: #067647; }
</style>
