<script setup>
// 侧栏：新诊断 / 知识库入口 + 历史运行列表（GET /api/runs）。
import { STATUS_LABELS } from '../api'

defineProps({
  runs: { type: Array, required: true },
  currentId: { type: String, default: '' },
  view: { type: String, default: 'chat' },
})
const emit = defineEmits(['select', 'new', 'kb', 'delete'])

function timeOf(iso) {
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  } catch {
    return ''
  }
}
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-name">🩺 FastAPI Doctor</div>
      <div class="brand-sub">Python Web 服务故障诊断 Agent</div>
    </div>
    <button class="new-chat-btn" @click="emit('new')">＋ 新的诊断</button>
    <button
      class="new-chat-btn kb-nav"
      :class="{ active: view === 'kb' }"
      @click="emit('kb')"
    >
      📚 知识库
    </button>
    <div class="history-title">历史诊断</div>
    <div class="history-list">
      <button
        v-for="run in runs"
        :key="run.run_id"
        class="history-item"
        :class="{ active: run.run_id === currentId }"
        @click="emit('select', run)"
      >
        <span class="dot" :class="run.status"></span>
        <span class="history-text">
          <span class="history-desc">{{ run.description || '（无描述）' }}</span>
        </span>
        <span class="history-time">{{ timeOf(run.created_at) }}</span>
        <span class="history-del" title="删除这条记录" @click.stop="emit('delete', run)">×</span>
      </button>
      <div class="history-empty" v-if="!runs.length">还没有诊断记录</div>
    </div>
    <div class="sidebar-foot">只提供建议，不执行任何命令</div>
  </aside>
</template>
