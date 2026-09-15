<script setup>
// 结构化诊断报告：结论、置信度、步骤、建议、引用与证据列表（§6.5）。
import { computed } from 'vue'

const props = defineProps({ result: { type: Object, required: true } })

const d = computed(() => props.result.diagnosis || {})
const review = computed(() => props.result.review || {})

const SOURCE_LABELS = {
  official_doc: '官方文档',
  incident_case: '故障案例',
  runbook: 'Runbook',
}

const confPercent = computed(() =>
  d.value.confidence != null ? Math.round(d.value.confidence * 100) : null
)

// citations 里可能是 URL（可点击）也可能是 doc_id（纯文本）。
function isUrl(text) {
  return typeof text === 'string' && /^https?:\/\//.test(text)
}
</script>

<template>
  <div class="report">
    <div class="report-head">
      <span class="badge">{{ review.passed ? '审查通过' : '审查有标注' }}</span>
      <span v-if="review.needs_confirmation" class="badge">
        {{ review.confirmed ? '危险建议已确认' : '危险建议被拒绝' }}
      </span>
    </div>

    <div class="report-section">
      <h4>最可能原因</h4>
      <div class="cause-text">{{ d.most_likely_cause }}</div>
      <div class="confidence" v-if="confPercent !== null">
        <div class="confidence-bar"><div :style="{ width: confPercent + '%' }"></div></div>
        <span style="font-size: 13px; color: var(--text-soft)">置信度 {{ confPercent }}%</span>
      </div>
    </div>

    <div class="report-section" v-if="review.issues?.length">
      <h4>审查标注</h4>
      <div class="issue-text" v-for="(issue, i) in review.issues" :key="i">⚠ {{ issue }}</div>
    </div>

    <div class="report-section" v-if="d.investigation_steps?.length">
      <h4>排查步骤</h4>
      <ol class="suggest">
        <li v-for="(s, i) in d.investigation_steps" :key="i">{{ s }}</li>
      </ol>
    </div>

    <div class="report-section" v-if="d.fix_suggestions?.length">
      <h4>修复建议（系统只展示建议，不会执行命令）</h4>
      <ol class="suggest">
        <li v-for="(s, i) in d.fix_suggestions" :key="i">{{ s }}</li>
      </ol>
    </div>

    <div class="report-section" v-if="d.verification?.length">
      <h4>验证方法</h4>
      <ol class="suggest">
        <li v-for="(s, i) in d.verification" :key="i">{{ s }}</li>
      </ol>
    </div>

    <div class="report-section" v-if="d.alternative_causes?.length">
      <h4>替代原因（可信度次之的可能）</h4>
      <ol class="suggest">
        <li v-for="(s, i) in d.alternative_causes" :key="i">{{ s }}</li>
      </ol>
    </div>

    <div class="report-section" v-if="d.citations?.length">
      <h4>参考资料</h4>
      <div class="cite-list">
        <a
          v-for="(c, i) in d.citations"
          :key="i"
          :href="isUrl(c) ? c : 'javascript:void(0)'"
          :target="isUrl(c) ? '_blank' : undefined"
          :rel="isUrl(c) ? 'noopener' : undefined"
          :style="isUrl(c) ? undefined : 'cursor: default'"
          >{{ c }}</a
        >
      </div>
    </div>

    <div class="report-section" v-if="result.evidence?.length">
      <h4>检索证据（{{ result.evidence.length }} 条）</h4>
      <details v-for="(e, i) in result.evidence" :key="i" class="evidence-item">
        <summary style="cursor: pointer">
          <div class="evidence-meta" style="display: inline-flex">
            <span class="chip">{{ SOURCE_LABELS[e.source_type] || e.source_type }}</span>
            <span>{{ e.doc_id }}</span>
            <a
              v-if="e.source_url"
              :href="e.source_url"
              target="_blank"
              rel="noopener"
              @click.stop
              style="color: var(--accent); text-decoration: none"
              >来源链接</a
            >
          </div>
        </summary>
        <div class="evidence-title" v-if="e.title || e.section">{{ e.title || e.section }}</div>
        <div class="evidence-content">{{ e.content }}</div>
      </details>
    </div>
  </div>
</template>
