<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import DimensionCard from "@/components/DimensionCard.vue";
import RadarChart from "@/components/RadarChart.vue";
import { averageEvidenceScore, publicScoreLabel } from "@/components/scoreFormat";
import { ApiError, downloadBlob } from "@/api/http";
import { getReport, getReportPdf } from "@/api/session";
import { DIMENSIONS, type AssessmentReport, type ReportDimension } from "@/types/contracts";

const dimensionSuggestions: Record<string, string> = {
  problem_definition: "继续明确目标、范围和需要核实的边界。",
  evidence_evaluation: "继续核实来源、样本范围和仍不确定的信息。",
  reasoning_argumentation: "继续区分结论、依据与可能改变结论的假设。",
  multiple_perspectives: "继续补充不同相关方和选择可能带来的影响。",
  integrative_decision: "继续把目标、约束、风险和回退条件放在一起权衡。",
  dynamic_adjustment: "继续提前写下会触发调整的信号和下一步行动。",
};

const route = useRoute();
const uuid = String(route.params.sessionUuid);
const report = ref<AssessmentReport | null>(null);
const loading = ref(true);
const downloading = ref(false);
const error = ref("");

const dimensions = computed<ReportDimension[]>(() => {
  const byKey = new Map((report.value?.dimensions ?? []).map((item) => [item.dimension_key, item]));
  return DIMENSIONS.map((meta) => {
    const item = byKey.get(meta.key);
    return {
      dimension_key: meta.key,
      dimension_name: item?.dimension_name || meta.name,
      status: item?.status || "unmeasured",
      score: item?.status === "sufficient" ? item.score : null,
      reason: item?.reason || "本次对话中没有获得足够的可追溯证据，因此不做判断。",
      strength: item?.strength || item?.reason || "本次证据有限，暂不形成该维度的优势判断。",
      suggestion: dimensionSuggestions[meta.key],
      evidences: item?.evidences ?? [],
    };
  });
});

const measuredDimensionCount = computed(() => dimensions.value.filter((item) => item.score !== null).length);
const overallScore = computed(() => averageEvidenceScore(dimensions.value.map((item) => item.score)));

async function loadReport() {
  loading.value = true;
  error.value = "";
  try {
    report.value = await getReport(uuid);
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "报告暂时无法读取。";
  } finally {
    loading.value = false;
  }
}

async function downloadPdf() {
  downloading.value = true;
  error.value = "";
  try {
    const blob = await getReportPdf(uuid);
    downloadBlob(blob, `思衡V6-自然访谈报告-${uuid.slice(0, 8)}.pdf`);
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "PDF 下载失败，请稍后重试。";
  } finally {
    downloading.value = false;
  }
}

onMounted(() => {
  if (localStorage.getItem("v6:last-session") === uuid) localStorage.removeItem("v6:last-session");
  void loadReport();
});
</script>

<template>
  <main class="report-page">
    <section v-if="loading" class="center-state"><span class="loading-ring" />正在读取报告…</section>
    <section v-else-if="report" class="report-shell">
      <header class="report-header">
        <a class="brand" href="/assessment" aria-label="开始新访谈"><span>思衡</span></a>
        <button class="secondary-button small" :disabled="downloading" @click="downloadPdf">{{ downloading ? "正在生成…" : "下载 PDF" }}</button>
      </header>

      <section class="report-summary">
        <div>
          <h1>访谈结果</h1>
          <p>{{ report.summary || "报告只呈现有原话支持的观察。证据有限或未充分测得，不代表能力不足。" }}</p>
          <div class="overall-score" aria-label="综合总分">
            <span>综合总分</span>
            <strong>{{ publicScoreLabel(overallScore) }}</strong>
            <small v-if="measuredDimensionCount">基于 {{ measuredDimensionCount }} 个证据充分维度计算</small>
            <small v-else>暂无可计算维度</small>
          </div>
          <p class="report-score-note">分数按百分制呈现；证据不足的维度不按 0 分计入综合总分。</p>
          <p v-if="report.manual_review_recommended" class="report-caution">部分维度证据有限，暂不显示分数。</p>
        </div>
        <figure class="report-radar">
          <RadarChart :dimensions="dimensions" />
          <figcaption>六维结果（百分制）</figcaption>
        </figure>
      </section>

      <section class="dimension-section">
        <div class="report-section-heading"><h2>六维结果</h2><p>展开后可查看优势、原话和建议。</p></div>
        <div class="dimension-list"><DimensionCard v-for="dimension in dimensions" :key="dimension.dimension_key" :dimension="dimension" /></div>
      </section>

      <footer class="report-disclaimer">
        <p>{{ report.disclaimer || "本报告只反映本次对话中可观察的内容，不等同于人格、智力或职业潜力判断，也不能替代专业决定。" }}</p>
      </footer>
    </section>
    <section v-else class="center-state error-state"><p>{{ error }}</p><button class="secondary-button" @click="loadReport">重新读取</button></section>
  </main>
</template>
