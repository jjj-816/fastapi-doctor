<script setup>
// 知识库浏览：按来源类型筛选、搜索，左列表右阅读本地原文（离线可用）。
import { computed, onMounted, ref } from 'vue'
import { api, SOURCE_LABELS } from '../api'

const TYPES = [
  { key: 'all', label: '全部' },
  { key: 'official_doc', label: SOURCE_LABELS.official_doc },
  { key: 'incident_case', label: SOURCE_LABELS.incident_case },
  { key: 'runbook', label: SOURCE_LABELS.runbook },
]

const docs = ref([])
const loading = ref(true)
const error = ref('')
const activeType = ref('all')
const query = ref('')
const selected = ref(null) // { doc_id, title, source_type, source_url, block_count, content }

const counts = computed(() => {
  const c = { all: docs.value.length }
  for (const t of TYPES.slice(1)) {
    c[t.key] = docs.value.filter((d) => d.source_type === t.key).length
  }
  return c
})

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  return docs.value.filter((d) => {
    if (activeType.value !== 'all' && d.source_type !== activeType.value) return false
    if (!q) return true
    return (
      d.doc_id.toLowerCase().includes(q) || String(d.title || '').toLowerCase().includes(q)
    )
  })
})

onMounted(async () => {
  try {
    docs.value = await api.listKbDocs()
  } catch (e) {
    error.value = e.message
  }
  loading.value = false
})

async function openDoc(doc) {
  try {
    selected.value = await api.getKbDoc(doc.doc_id)
  } catch (e) {
    error.value = e.message
  }
}
</script>

<template>
  <div class="kb-page">
    <div class="kb-side">
      <div class="kb-head">
        <h3>知识库</h3>
        <input v-model="query" class="kb-search" placeholder="搜索文档 ID / 标题" />
        <div class="kb-types">
          <button
            v-for="t in TYPES"
            :key="t.key"
            class="kb-type"
            :class="{ active: activeType === t.key }"
            @click="activeType = t.key"
          >
            {{ t.label }}<span class="kb-count">{{ counts[t.key] }}</span>
          </button>
        </div>
      </div>
      <div class="kb-list">
        <div class="kb-hint" v-if="loading">加载中…</div>
        <div class="kb-hint" v-else-if="error">{{ error }}</div>
        <div class="kb-hint" v-else-if="!filtered.length">没有匹配的文档</div>
        <button
          v-for="doc in filtered"
          :key="doc.doc_id"
          class="kb-item"
          :class="{ active: selected && selected.doc_id === doc.doc_id }"
          @click="openDoc(doc)"
        >
          <span class="kb-item-title">{{ doc.title }}</span>
          <span class="kb-item-meta">
            <span class="chip">{{ SOURCE_LABELS[doc.source_type] || doc.source_type }}</span>
            <span>{{ doc.block_count }} 段</span>
          </span>
        </button>
      </div>
    </div>

    <div class="kb-reader">
      <template v-if="selected">
        <h2>{{ selected.title }}</h2>
        <p class="kb-reader-meta">
          {{ selected.doc_id }} · {{ selected.block_count }} 段
          <span v-if="selected.source_url"> · 原始来源 {{ selected.source_url }}</span>
        </p>
        <pre class="kb-reader-content">{{ selected.content }}</pre>
      </template>
      <div class="kb-reader-empty" v-else>
        <p class="kb-reader-main">← 从左侧选择一篇文档</p>
        <p class="kb-reader-sub">内容全部来自本地知识库（向量库 + 父块存储），离线部署同样可用</p>
      </div>
    </div>
  </div>
</template>
