<script setup>
import { computed, reactive } from 'vue'

const props = defineProps({
  events: { type: Array, required: true },
  running: { type: Boolean, default: false },
})

const NODE_LABELS = {
  analyze_input: '分析故障输入',
  clarify_if_needed: '检查信息完整性',
  plan: '制定检索计划',
  retrieve: '检索知识库',
  grade_evidence: '证据评分',
  rewrite_query: '改写检索词重试',
  diagnose: '生成诊断',
  review: '审查结论',
}

// 每个节点完成时发出的标志性事件；用于判断步骤"进行中/已完成"。
const DONE_EVENTS = {
  analyze_input: 'input_analyzed',
  plan: 'plan_created',
  retrieve: 'evidence_found',
  grade_evidence: 'evidence_graded',
  rewrite_query: 'query_rewritten',
  diagnose: 'diagnosis_generated',
  review: 'review_completed',
}

function detailFor(ev) {
  const p = ev.payload || {}
  switch (ev.type) {
    case 'input_analyzed': {
      const items = []
      if (p.fault_info?.component)
        items.push({ label: `组件: ${p.fault_info.component}`, cls: 'accent' })
      if (p.fault_info?.exception_type)
        items.push({ label: `异常: ${p.fault_info.exception_type}`, cls: 'accent' })
      if (p.fault_info?.http_status)
        items.push({ label: `HTTP ${p.fault_info.http_status}`, cls: 'accent' })
      if (p.fault_info?.missing_information?.length)
        items.push({
          label: `待澄清: ${p.fault_info.missing_information.join('、')}`,
          cls: 'warn',
        })
      return items.length ? { kind: 'chips', items } : null
    }
    case 'plan_created':
      return p.search_queries?.length
        ? { kind: 'chips', items: p.search_queries.map((q) => ({ label: q, cls: '' })) }
        : null
    case 'tool_called':
      return p.queries?.length
        ? { kind: 'chips', items: p.queries.map((q) => ({ label: `检索: ${q}`, cls: '' })) }
        : null
    case 'evidence_found':
      return p.doc_ids?.length
        ? { kind: 'chips', items: p.doc_ids.map((id) => ({ label: id, cls: 'accent' })) }
        : { kind: 'text', text: '未检索到证据' }
    case 'evidence_graded':
      return {
        kind: 'text',
        text: p.sufficient
          ? `证据充分：${p.reason || ''}`
          : `证据不足：${p.reason || ''}${
              p.missing_terms?.length ? `（缺失: ${p.missing_terms.join('、')}）` : ''
            }`,
        warn: !p.sufficient,
      }
    case 'query_rewritten':
      return { kind: 'text', text: `第 ${p.retry_count} 轮改写：${p.query}` }
    case 'diagnosis_generated':
      return {
        kind: 'text',
        text: `主因（置信度 ${p.confidence}）：${p.most_likely_cause}`,
      }
    case 'diagnosis_retried':
      return { kind: 'text', text: '发现无效引用，已自动重试修正', warn: true }
    case 'review_completed': {
      if (p.needs_confirmation)
        return {
          kind: 'text',
          text: p.approved ? '危险建议已由用户确认' : '危险建议被拒绝',
          warn: true,
        }
      return {
        kind: 'text',
        text: p.passed ? '审查通过' : '审查发现问题，已如实标注',
        warn: !p.passed,
      }
    }
    default:
      return null
  }
}

// 事件归组：node_started 开启新步骤，其余事件挂到当前步骤明细。
const steps = computed(() => {
  const list = []
  for (const ev of props.events) {
    if (ev.type === 'node_started') {
      list.push({
        node: ev.payload?.node,
        label: NODE_LABELS[ev.payload?.node] || ev.payload?.node || '执行中',
        details: [],
        doneEvent: DONE_EVENTS[ev.payload?.node],
        firstSeq: ev.seq,
      })
      continue
    }
    const step = list[list.length - 1]
    if (!step) continue
    const detail = detailFor(ev)
    if (detail) step.details.push(detail)
  }
  return list
})

function nodeDone(step) {
  if (!step.doneEvent) return true // clarify_if_needed 无独立完成事件
  return props.events.some((e) => e.type === step.doneEvent && e.seq >= step.firstSeq)
}

const openState = reactive({})

function toggle(i) {
  openState[i] = !openState[i]
}

function isOpen(i, step, done) {
  if (openState[i] !== undefined) return openState[i]
  if (step.details.length === 0) return false
  if (!done && props.running) return true // 进行中的步骤默认展开
  return i === steps.value.length - 1
}

const view = computed(() =>
  steps.value.map((step, i) => {
    const done = nodeDone(step)
    return {
      ...step,
      i,
      spinning: !done && props.running,
      open: isOpen(i, step, done),
    }
  })
)
</script>

<template>
  <div class="steps">
    <div class="step" v-for="s in view" :key="s.i">
      <button class="step-head" @click="toggle(s.i)">
        <span class="step-icon">
          <span v-if="s.spinning" class="spinner"></span>
          <span v-else style="color: var(--green)">✓</span>
        </span>
        <span class="step-title">{{ s.label }}</span>
        <span class="step-detail-count" v-if="s.details.length">{{ s.details.length }} 项</span>
        <span class="step-detail-count" v-if="s.details.length">{{ s.open ? '收起' : '展开' }}</span>
      </button>
      <div class="step-body" v-if="s.open && s.details.length">
        <template v-for="(d, j) in s.details" :key="j">
          <div
            class="detail"
            v-if="d.kind === 'text'"
            :style="d.warn ? 'color: var(--amber)' : ''"
          >
            {{ d.text }}
          </div>
          <div class="chips" v-else-if="d.kind === 'chips'">
            <span class="chip" :class="c.cls" v-for="(c, k) in d.items" :key="k">{{
              c.label
            }}</span>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>
