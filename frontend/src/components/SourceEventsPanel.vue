<template>
  <section class="source-panel">
    <header class="source-head">
      <div>
        <h3>来源事件</h3>
        <p class="source-desc">
          以下内容直接回读审计事件快照，不读取业务对象当前值，前后值与复核信息以归档时为准。
        </p>
      </div>
      <button class="btn ghost" type="button" @click="reload">刷新来源事件</button>
    </header>

    <div v-if="loading" class="source-empty">正在读取来源事件…</div>
    <div v-else-if="errorMessage" class="source-empty error-text">{{ errorMessage }}</div>
    <div v-else-if="!events.length" class="source-empty">该业务记录尚无关联审计事件</div>
    <table v-else class="data-table source-table">
      <thead>
        <tr>
          <th>记录编号</th>
          <th>修订序号</th>
          <th>变更类型</th>
          <th>变更前值</th>
          <th>变更后值</th>
          <th>操作人</th>
          <th>操作时间</th>
          <th>复核人 / 复核时间</th>
          <th>复核状态</th>
          <th>归档指纹</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="event in events" :key="String(event.id)">
          <td>{{ event['记录编号'] }}</td>
          <td>{{ event['修订序号'] }}</td>
          <td>{{ event['变更类型'] }}</td>
          <td>{{ event['变更前值'] ?? '—' }}</td>
          <td>{{ event['变更后值'] ?? '—' }}</td>
          <td>
            {{ event['操作人'] ?? '—' }}
            <span v-if="event['操作人账号快照']" class="snapshot">快照 {{ event['操作人账号快照'] }}</span>
          </td>
          <td>{{ event['操作时间'] ?? '—' }}</td>
          <td>
            <span :class="{ 'missing-text': event['复核人'] === null }">{{ event['复核人'] ?? '缺失' }}</span>
            /
            <span :class="{ 'missing-text': event['复核时间'] === null }">{{ event['复核时间'] ?? '缺失' }}</span>
          </td>
          <td>
            {{ event['复核状态'] }}
            <span v-if="event['只读']" class="readonly-tag">只读</span>
          </td>
          <td class="fingerprint-cell" :title="fingerprintOf(event)">
            {{ event['归档指纹'] ? `${String(event['归档指纹']).slice(0, 12)}…` : '—' }}
            <span class="fingerprint-note">{{ event['指纹校验'] }}</span>
          </td>
        </tr>
      </tbody>
    </table>

    <footer v-if="events.length" class="source-foot">
      <span>共 {{ events.length }} 条来源事件；归档事件保持只读，后续修订以新记录出现。</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import { request } from '@/api/client'

type SourceEvent = Record<string, string | number | boolean | null>

const props = defineProps<{
  endpoint: string
  entryId: number | null
}>()

const events = ref<SourceEvent[]>([])
const loading = ref(false)
const errorMessage = ref('')

function fingerprintOf(event: SourceEvent): string | undefined {
  const value = event['归档指纹']
  return value === null || value === undefined ? undefined : String(value)
}

async function reload() {
  errorMessage.value = ''
  events.value = []
  if (props.entryId === null || props.entryId === undefined) {
    return
  }
  loading.value = true
  try {
    const response = await request(`${props.endpoint}/${props.entryId}/events`)
    if (!response.ok) {
      throw new Error('来源事件读取失败')
    }
    const payload = await response.json()
    events.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '来源事件读取失败'
  } finally {
    loading.value = false
  }
}

watch(() => [props.entryId, props.endpoint], reload)
onMounted(reload)
</script>

<style scoped>
.source-panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.source-head { display: flex; justify-content: space-between; align-items: flex-start; }
.source-head h3 { margin: 0; font-size: 15px; }
.source-desc { margin: 4px 0 0; font-size: 12px; color: var(--muted); }
.source-table { margin-top: 10px; }
.source-empty { padding: 14px 4px; color: var(--muted); font-size: 13px; }
.source-foot { margin-top: 8px; font-size: 12px; color: var(--muted); }
.snapshot { display: block; font-size: 11px; color: var(--muted); }
.readonly-tag { margin-left: 6px; padding: 1px 6px; border-radius: 999px; background: #eef2f7; color: #475569; font-size: 11px; }
.missing-text { color: #b42318; }
.fingerprint-cell { font-family: ui-monospace, monospace; font-size: 12px; }
.fingerprint-note { display: block; font-family: inherit; font-size: 11px; color: var(--muted); }
</style>
