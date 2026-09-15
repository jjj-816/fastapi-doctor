// 后端 API 封装：REST + SSE 事件流。

const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body.detail) detail = body.detail
    } catch {
      /* 非 JSON 响应体，保留状态行 */
    }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  listRuns: (limit = 50) => request(`/runs?limit=${limit}`),
  getRun: (runId) => request(`/runs/${runId}`),
  createRun: (payload) =>
    request('/runs', { method: 'POST', body: JSON.stringify(payload) }),
  resumeRun: (runId, payload) =>
    request(`/runs/${runId}/resume`, { method: 'POST', body: JSON.stringify(payload) }),
  sendFeedback: (runId, payload) =>
    request(`/runs/${runId}/feedback`, { method: 'POST', body: JSON.stringify(payload) }),
  listKbDocs: () => request('/kb/docs'),
  // 应用内阅读需要 JSON；默认浏览器导航 Accept 会拿到 HTML 页。
  getKbDoc: (docId) =>
    request(`/kb/doc/${encodeURIComponent(docId)}`, {
      headers: { Accept: 'application/json' },
    }),
  // 文件上传走 multipart，不能用 request() 的 JSON Content-Type。
  uploadKbDocs: async (files) => {
    const body = new FormData()
    for (const f of files) body.append('files', f)
    const res = await fetch(BASE + '/kb/upload', { method: 'POST', body })
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`
      try {
        const err = await res.json()
        if (err.detail) detail = err.detail
      } catch {
        /* 非 JSON 响应体，保留状态行 */
      }
      throw new Error(detail)
    }
    return res.json()
  },
}

// 后端会出现的全部 SSE 事件名（EventSource 按 event 名分发）。
const EVENT_TYPES = [
  'run_started',
  'run_resumed',
  'node_started',
  'input_analyzed',
  'plan_created',
  'tool_called',
  'evidence_found',
  'evidence_graded',
  'query_rewritten',
  'diagnosis_generated',
  'diagnosis_retried',
  'review_completed',
  'run_completed',
  'run_failed',
  'clarification_required',
  'confirmation_required',
]

// 终态事件：流在这些事件上结束（与后端 STREAM_TERMINAL_EVENTS 对应）。
const TERMINAL_EVENTS = new Set([
  'run_completed',
  'run_failed',
  'clarification_required',
  'confirmation_required',
])

/**
 * 打开某次运行的事件流。
 * onEvent 收到 {type, payload, seq}；流在终态事件后自动关闭并调 onClose。
 * 断线时 EventSource 自动带 Last-Event-ID 重连，后端按序补发。
 */
export function openEventStream(runId, { after = 0, onEvent, onClose } = {}) {
  const source = new EventSource(`${BASE}/runs/${runId}/events?after=${after}`)
  const handler = (e) => {
    let payload = {}
    try {
      payload = e.data ? JSON.parse(e.data) : {}
    } catch {
      /* 心跳等非 JSON 数据忽略 */
    }
    if (onEvent) onEvent({ type: e.type, payload, seq: Number(e.lastEventId) || 0 })
    if (TERMINAL_EVENTS.has(e.type)) {
      source.close()
      if (onClose) onClose()
    }
  }
  for (const type of EVENT_TYPES) source.addEventListener(type, handler)
  // 断线由 EventSource 自动重连；终态已在 handler 里 close。
  source.onerror = () => {}
  return source
}

export const STATUS_LABELS = {
  running: '诊断中',
  waiting_clarification: '等待补充信息',
  waiting_confirmation: '等待风险确认',
  needs_clarification: '信息不足',
  completed: '已完成',
  failed: '失败',
}

export const SOURCE_LABELS = {
  official_doc: '官方文档',
  incident_case: '故障案例',
  runbook: 'Runbook',
}
