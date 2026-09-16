<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { api, openEventStream, STATUS_LABELS } from './api'
import SidebarHistory from './components/SidebarHistory.vue'
import Composer from './components/Composer.vue'
import ProcessTimeline from './components/ProcessTimeline.vue'
import ClarifyCard from './components/ClarifyCard.vue'
import ConfirmCard from './components/ConfirmCard.vue'
import DiagnosisCard from './components/DiagnosisCard.vue'
import FeedbackCard from './components/FeedbackCard.vue'
import KnowledgeBase from './components/KnowledgeBase.vue'

const runs = ref([])
const current = ref(null) // { id, description, logs, code, config, status, events, result, error }
const view = ref('chat') // 主区域视图：chat = 诊断对话，kb = 知识库浏览
const lastSeq = ref(0)
const supplements = ref([]) // 本次会话里用户补充的内容（用于聊天气泡展示）
const feedbackDone = ref(false)
const actionBusy = ref(false)
const chatScroll = ref(null)
const prefill = ref(null)
const es = ref(null) // 当前 SSE 流的 EventSource 句柄

const clarifyEvent = computed(() =>
  [...(current.value?.events || [])].reverse().find((e) => e.type === 'clarification_required')
)
const confirmEvent = computed(() =>
  [...(current.value?.events || [])].reverse().find((e) => e.type === 'confirmation_required')
)
const running = computed(() => current.value?.status === 'running')
const statusLabel = computed(() =>
  current.value ? (STATUS_LABELS[current.value.status] || current.value.status) : ''
)

async function refreshRuns() {
  try {
    runs.value = await api.listRuns()
  } catch {
    /* 侧栏刷新失败不打断主流程 */
  }
}

function closeStream() {
  if (es.value) {
    es.value.close()
    es.value = null
  }
}

function scrollToBottom() {
  nextTick(() => {
    const el = chatScroll.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function onStreamEvent(ev) {
  const c = current.value
  if (!c) return
  if (ev.seq && ev.seq > lastSeq.value) lastSeq.value = ev.seq
  c.events.push(ev)
  // 暂停/恢复事件只在语义成立的当前状态下生效：回放历史事件时不得
  // 覆盖快照带来的真实状态（否则已完成运行会错标成"等待补充信息"）。
  if (ev.type === 'clarification_required') {
    if (c.status === 'running') c.status = 'waiting_clarification'
  } else if (ev.type === 'confirmation_required') {
    if (c.status === 'running') c.status = 'waiting_confirmation'
  } else if (ev.type === 'run_resumed') {
    if (c.status === 'waiting_clarification' || c.status === 'waiting_confirmation') {
      c.status = 'running'
    }
  } else if (ev.type === 'run_completed' || ev.type === 'run_failed') {
    loadSnapshot(c.id)
  }
  scrollToBottom()
}

function openStream(runId, after = 0) {
  closeStream()
  es.value = openEventStream(runId, { after, onEvent: onStreamEvent })
}

async function loadSnapshot(runId) {
  try {
    const snap = await api.getRun(runId)
    const c = current.value
    if (!c || c.id !== runId) return
    c.status = snap.status
    c.error = snap.error
    c.result = snap.result
    refreshRuns()
  } catch {
    /* 快照失败时保留事件流信息 */
  }
  scrollToBottom()
}

function resetSessionState() {
  closeStream()
  lastSeq.value = 0
  supplements.value = []
  feedbackDone.value = false
}

async function deleteRun(run) {
  if (!confirm(`删除这条诊断记录？\n${(run.description || '（无描述）').slice(0, 50)}`)) return
  try {
    await api.deleteRun(run.run_id)
    if (current.value?.id === run.run_id) {
      resetSessionState()
      current.value = null
    }
    refreshRuns()
  } catch (e) {
    alert(`删除失败：${e.message}`)
  }
}

async function selectRun(run) {
  view.value = 'chat'
  resetSessionState()
  current.value = {
    id: run.run_id,
    description: run.description,
    logs: '',
    code: '',
    config: '',
    status: run.status,
    events: [],
    result: null,
    error: run.error || null,
  }
  await loadSnapshot(run.run_id)
  openStream(run.run_id, 0) // 全量回放历史事件，流在终态事件自动关闭
  scrollToBottom()
}

async function startRun(form) {
  actionBusy.value = true
  try {
    const created = await api.createRun(form)
    resetSessionState()
    current.value = { id: created.run_id, ...form, status: created.status, events: [], result: null, error: null }
    refreshRuns()
    openStream(created.run_id, 0)
    scrollToBottom()
  } catch (e) {
    alert(`创建诊断失败：${e.message}`)
  }
  actionBusy.value = false
}

async function resumeClarify(answers) {
  actionBusy.value = true
  try {
    await api.resumeRun(current.value.id, { answers })
    supplements.value.push(
      Object.keys(answers).length
        ? Object.entries(answers)
            .map(([k, v]) => `[${k}] ${v}`)
            .join('\n')
        : '跳过补充，直接继续诊断'
    )
    await loadSnapshot(current.value.id)
    openStream(current.value.id, lastSeq.value)
    scrollToBottom()
  } catch (e) {
    alert(`提交失败：${e.message}`)
  }
  actionBusy.value = false
}

async function decideConfirm(approved) {
  actionBusy.value = true
  try {
    await api.resumeRun(current.value.id, { approved })
    supplements.value.push(approved ? '已确认：接受包含危险操作的建议' : '已拒绝：不接受包含危险操作的建议')
    await loadSnapshot(current.value.id)
    openStream(current.value.id, lastSeq.value)
    scrollToBottom()
  } catch (e) {
    alert(`提交失败：${e.message}`)
  }
  actionBusy.value = false
}

async function sendFeedback(payload) {
  try {
    await api.sendFeedback(current.value.id, payload)
    feedbackDone.value = true
  } catch (e) {
    alert(`反馈提交失败：${e.message}`)
  }
}

const EXAMPLES = [
  {
    description: 'FastAPI 服务在容器内访问 PostgreSQL 报错，宿主机直连数据库正常',
    logs:
      'sqlalchemy.exc.OperationalError: connection to server at "localhost" (::1), port 5432 failed: Connection refused',
  },
  {
    description: '同事拉代码后服务启动就崩，我本机一直正常',
    logs:
      'pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings\ndatabase_url\n  Field required [type=missing, input_value={}]',
  },
  {
    description: '服务运行一段时间后批量 500，重启暂时恢复，过阵子又复发',
    logs:
      'sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00',
  },
]

function useExample(i) {
  prefill.value = { ...EXAMPLES[i], stamp: Date.now() }
}

onMounted(refreshRuns)
</script>

<template>
  <div class="layout">
    <SidebarHistory
      :runs="runs"
      :current-id="current?.id || ''"
      :view="view"
      @select="selectRun"
      @delete="deleteRun"
      @kb="view = 'kb'"
      @new="
        () => {
          resetSessionState()
          current = null
          view = 'chat'
        }
      "
    />

    <div class="main">
      <!-- 知识库浏览 -->
      <KnowledgeBase v-if="view === 'kb'" />

      <template v-else>
      <div class="chat-scroll" ref="chatScroll">
        <!-- 欢迎页 -->
        <div class="welcome" v-if="!current">
          <h1>FastAPI<span class="plus"> Doctor</span></h1>
          <p>提交故障描述与日志，Agent 完成分析 → 检索知识库 → 证据评分 → 诊断 → 审查</p>
          <div class="feature-grid">
            <div class="feature">
              <b>🔍 混合检索</b>
              <span>官方文档 + 历史案例 + Runbook 三源检索，引用可溯源</span>
            </div>
            <div class="feature">
              <b>🧠 证据评分与重写</b>
              <span>LLM 评估证据充分性，不足时自动改写检索词重试</span>
            </div>
            <div class="feature">
              <b>🙋 信息澄清</b>
              <span>信息不足时主动追问，补充后从断点继续</span>
            </div>
            <div class="feature">
              <b>⚠️ 危险操作确认</b>
              <span>修复建议含危险命令时暂停等待人工批准</span>
            </div>
          </div>
          <div class="examples">
            <button class="example-btn" v-for="(ex, i) in EXAMPLES" :key="i" @click="useExample(i)">
              {{ ex.description }}
            </button>
          </div>
        </div>

        <!-- 会话 -->
        <div class="chat-inner" v-else>
          <div
            style="display: flex; align-items: center; gap: 10px; margin-bottom: 16px; font-size: 13px; color: var(--text-soft)"
          >
            <span class="dot" :class="current.status"></span>
            <span>{{ statusLabel }}</span>
            <span v-if="running" class="pulse-dot"></span>
          </div>

          <!-- 用户初始提问 -->
          <div class="user-bubble">
            {{ current.description }}
            <div class="attach" v-if="current.logs || current.code || current.config">
              <div v-if="current.logs" style="margin-bottom: 6px">
                <b>日志：</b><span style="white-space: pre-wrap">{{ current.logs }}</span>
              </div>
              <div v-if="current.code" style="margin-bottom: 6px">
                <b>代码：</b><span style="white-space: pre-wrap">{{ current.code }}</span>
              </div>
              <div v-if="current.config">
                <b>配置：</b><span style="white-space: pre-wrap">{{ current.config }}</span>
              </div>
            </div>
          </div>

          <!-- Agent 执行时间线 -->
          <div class="agent-block">
            <div class="agent-label"><span class="avatar">🩺</span>FastAPI Doctor 正在诊断</div>
            <ProcessTimeline :events="current.events" :running="running" />
          </div>

          <!-- 用户补充（澄清/确认） -->
          <div v-for="(text, i) in supplements" :key="i">
            <div class="resume-divider">已补充信息，继续诊断</div>
            <div class="user-bubble" style="white-space: pre-wrap">{{ text }}</div>
          </div>

          <!-- 澄清面板 -->
          <ClarifyCard
            v-if="current.status === 'waiting_clarification' && clarifyEvent"
            :payload="clarifyEvent.payload"
            :busy="actionBusy"
            @submit="resumeClarify"
          />

          <!-- 风险确认面板 -->
          <ConfirmCard
            v-if="current.status === 'waiting_confirmation' && confirmEvent"
            :payload="confirmEvent.payload"
            :busy="actionBusy"
            @decide="decideConfirm"
          />

          <!-- 失败横幅 -->
          <div class="error-banner" v-if="current.status === 'failed' && current.error">
            诊断失败：{{ current.error }}
          </div>

          <!-- 诊断报告 + 反馈 -->
          <DiagnosisCard v-if="current.result" :result="current.result" />
          <FeedbackCard
            v-if="current.result && current.status === 'completed' && !feedbackDone"
            :busy="actionBusy"
            @submit="sendFeedback"
          />
        </div>
      </div>

      <Composer
        :busy="actionBusy || running"
        :prefill="prefill"
        @submit="startRun"
      />
      </template>
    </div>
  </div>
</template>
