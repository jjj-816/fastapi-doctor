<script setup>
import { reactive, ref, watch } from 'vue'

// 输入区：故障描述必填；日志 / 代码 / 配置在"补充材料"里折叠。
const props = defineProps({
  busy: { type: Boolean, default: false },
  prefill: { type: Object, default: null }, // 欢迎页示例一键填充
})
const emit = defineEmits(['submit'])

const description = ref('')
const advanced = ref(false)
const form = reactive({ logs: '', code: '', config: '' })

watch(
  () => props.prefill,
  (v) => {
    if (!v) return
    description.value = v.description || ''
    form.logs = v.logs || ''
    form.code = v.code || ''
    form.config = v.config || ''
    advanced.value = Boolean(v.logs || v.code || v.config)
  }
)

function submit() {
  const desc = description.value.trim()
  if (!desc || props.busy) return
  emit('submit', {
    description: desc,
    logs: form.logs.trim(),
    code: form.code.trim(),
    config: form.config.trim(),
  })
  description.value = ''
  form.logs = ''
  form.code = ''
  form.config = ''
  advanced.value = false
}

function onKeydown(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') submit()
}
</script>

<template>
  <div class="composer-wrap">
    <div class="composer">
      <div class="adv-grid" v-if="advanced">
        <div>
          <label>错误日志</label>
          <textarea v-model="form.logs" placeholder="粘贴异常 / traceback"></textarea>
        </div>
        <div>
          <label>相关代码</label>
          <textarea v-model="form.code" placeholder="粘贴相关代码片段（选填）"></textarea>
        </div>
        <div style="grid-column: 1 / -1">
          <label>相关配置（请先脱敏）</label>
          <textarea v-model="form.config" placeholder="粘贴脱敏后的配置（选填）"></textarea>
        </div>
      </div>
      <textarea
        v-model="description"
        placeholder="描述故障现象，例如：FastAPI 服务在容器内访问 PostgreSQL 报 Connection refused…"
        @keydown="onKeydown"
      ></textarea>
      <div class="composer-foot">
        <button class="adv-toggle" @click="advanced = !advanced">
          {{ advanced ? '▾ 收起补充材料' : '▸ 补充材料（日志 / 代码 / 配置）' }}
        </button>
        <button class="send-btn" :disabled="busy || !description.trim()" @click="submit">
          <span
            v-if="busy"
            class="spinner"
            style="border-top-color: #fff; border-color: #ffffff88"
          ></span>
          {{ busy ? '诊断中…' : '开始诊断' }}
        </button>
      </div>
    </div>
    <div class="composer-hint" style="text-align: center; margin-top: 6px">
      Ctrl + Enter 发送 · 凭据请先脱敏，系统发送前也会自动遮蔽密钥/密码
    </div>
  </div>
</template>
