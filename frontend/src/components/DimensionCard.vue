<script setup lang="ts">
import { computed, ref } from "vue";
import type { ReportDimension } from "@/types/contracts";
import { publicScoreLabel } from "./scoreFormat";

const props = defineProps<{
  dimension: ReportDimension;
}>();
const open = ref(false);
const strength = computed(() => props.dimension.strength || props.dimension.reason);

const statusLabels = {
  sufficient: "证据充分",
  limited: "证据有限",
  unmeasured: "未充分测得",
};

function evidenceSource(source?: string, turnIndex?: number, answerOrdinal?: number) {
  const sourceLabel = source === "user" || !source ? "用户原话" : source;
  if (answerOrdinal !== undefined) return `第 ${answerOrdinal} 次回答 · ${sourceLabel}`;
  return turnIndex === undefined ? sourceLabel : `对话记录 #${turnIndex} · ${sourceLabel}`;
}
</script>

<template>
  <article class="dimension-card" :class="`status-${dimension.status}`">
    <button type="button" class="dimension-head" :aria-expanded="open" @click="open = !open">
      <span>
        <strong>{{ dimension.dimension_name }}</strong>
        <small>{{ statusLabels[dimension.status] }}</small>
      </span>
      <span class="dimension-score">{{ publicScoreLabel(dimension.score) }}</span>
      <span aria-hidden="true" class="chevron">{{ open ? "−" : "+" }}</span>
    </button>
    <div v-if="open" class="dimension-body">
      <section>
        <h3>优势</h3>
        <p>{{ strength }}</p>
      </section>
      <section>
        <h3>你的原话</h3>
        <blockquote v-for="(evidence, index) in dimension.evidences" :key="`${evidence.turn_index}-${index}`">
          “{{ evidence.quote }}”
          <small>{{ evidenceSource(evidence.source_type, evidence.turn_index, evidence.answer_ordinal) }}</small>
        </blockquote>
        <p v-if="!dimension.evidences.length" class="muted">本次没有形成足以引用的有效证据。</p>
      </section>
      <section>
        <h3>建议</h3>
        <p>{{ dimension.suggestion }}</p>
      </section>
    </div>
  </article>
</template>
