<script setup>
import { reactive, ref } from 'vue'

// 用户反馈：星级 + 根因/解决方案确认或修正。
const props = defineProps({ busy: { type: Boolean, default: false } })
const emit = defineEmits(['submit'])

const rating = ref(0)
const hover = ref(0)
const form = reactive({ root_cause: '', solution: '' })
const done = ref(false)

function submit() {
  if (!rating.value) return
  emit('submit', {
    rating: rating.value,
    root_cause: form.root_cause.trim(),
    solution: form.solution.trim(),
  })
  done.value = true
}
</script>

<template>
  <div class="feedback-card" v-if="!done">
    <div class="card-title">这次诊断对你有帮助吗？</div>
    <div class="stars">
      <span
        v-for="n in 5"
        :key="n"
        :class="{ on: n <= (hover || rating) }"
        @mouseenter="hover = n"
        @mouseleave="hover = 0"
        @click="rating = n"
        >★</span
      >
      <span style="font-size: 13px; color: var(--text-soft); margin-left: 8px; align-self: center">
        {{ rating ? `${rating} / 5` : '点击评分' }}
      </span>
    </div>
    <template v-if="rating">
      <input v-model="form.root_cause" placeholder="实际根因（选填，帮助我们补充知识库）" />
      <input v-model="form.solution" placeholder="实际解决方案（选填）" />
      <div class="btn-row">
        <button class="btn-primary" :disabled="busy || !rating" @click="submit">提交反馈</button>
      </div>
    </template>
  </div>
  <div class="feedback-card" v-else style="color: var(--green)">✓ 感谢反馈，已记录。</div>
</template>
