<script setup>
import { computed, reactive } from 'vue'

// 澄清面板：payload.questions[i] 与 payload.missing[i] 一一对应，
// 提交 {字段名: 补充文本} 给 POST /resume 的 answers。
const props = defineProps({
  payload: { type: Object, required: true },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['submit'])

const FIELD_HINTS = {
  component: '例如：数据库访问 / 启动阶段 / 请求处理 / 异步任务',
  logs: '粘贴异常类型和 traceback 最后 20 行，没有可留空',
  code: '粘贴相关代码片段，没有可留空',
  config: '粘贴脱敏后的相关配置，没有可留空',
}

const answers = reactive({})

// questions 与 missing 平行配对；missing 缺失时退回常见键名顺序。
const fields = computed(() => {
  const keys = props.payload.missing || []
  return keys.map((key, i) => ({ key, question: props.payload.questions?.[i] || '' }))
})

function submit() {
  const filled = {}
  for (const { key } of fields.value) {
    const value = (answers[key] || '').trim()
    if (value) filled[key] = value
  }
  if (Object.keys(filled).length === 0) return
  emit('submit', filled)
}

// 全部留空跳过：后端把这些字段记为"没有该信息"，不再重复追问。
function skip() {
  emit('submit', {})
}
</script>

<template>
  <div class="clarify-card">
    <div class="card-title">🤔 需要补充一些信息（第 {{ payload.round }} 轮）</div>
    <div v-for="f in fields" :key="f.key">
      <div class="q">{{ f.question }}</div>
      <textarea
        v-if="f.key === 'logs'"
        v-model="answers[f.key]"
        rows="4"
        :placeholder="FIELD_HINTS[f.key] || '请输入'"
      ></textarea>
      <input v-else v-model="answers[f.key]" :placeholder="FIELD_HINTS[f.key] || '请输入'" />
      <div class="hint" v-if="FIELD_HINTS[f.key]">{{ FIELD_HINTS[f.key] }}</div>
    </div>
    <div class="btn-row">
      <button class="btn-primary" :disabled="busy" @click="submit">提交并继续诊断</button>
      <button class="btn-safe" :disabled="busy" @click="skip">没有这些信息，跳过继续</button>
    </div>
  </div>
</template>
