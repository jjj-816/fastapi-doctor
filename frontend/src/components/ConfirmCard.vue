<script setup>
// 风险确认面板：修复建议包含危险操作时暂停，等待用户批准或拒绝。
defineProps({
  payload: { type: Object, required: true },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['decide'])
</script>

<template>
  <div class="confirm-card">
    <div class="card-title">⚠️ 修复建议包含危险操作，需要你确认</div>
    <div v-if="payload.report" style="font-size: 14px">
      诊断主因：{{ payload.report.most_likely_cause }}
    </div>
    <div v-for="(cmd, i) in payload.dangerous_commands" :key="i" class="danger-cmd">
      {{ cmd }}
    </div>
    <div style="font-size: 13px; color: var(--text-soft)">
      批准后系统只会展示该建议，不会实际执行任何命令。
    </div>
    <div class="btn-row">
      <button class="btn-danger" :disabled="busy" @click="emit('decide', true)">
        确认（我已知晓风险）
      </button>
      <button class="btn-safe" :disabled="busy" @click="emit('decide', false)">
        拒绝该建议
      </button>
    </div>
  </div>
</template>
