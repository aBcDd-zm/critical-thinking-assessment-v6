<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { finalizeAdminSession, getAdminSession, saveExpertScores, updateReview } from "@/api/admin";
import { useAdminAuth } from "@/composables/useAdminAuth";
import {
  DIMENSIONS,
  answerCount,
  type AdminEvidenceItem,
  type AdminSessionDetail,
  type ExpertScore,
  type ReviewStatus,
} from "@/types/contracts";

type ReviewTab = "conversation" | "interview" | "trace" | "review";

const route = useRoute();
const uuid = String(route.params.sessionUuid);
const auth = useAdminAuth();
const detail = ref<AdminSessionDetail | null>(null);
const activeTab = ref<ReviewTab>("conversation");
const loading = ref(true);
const saving = ref(false);
const reportRetrying = ref(false);
const error = ref("");
const message = ref("");
const review = reactive<{ review_status: ReviewStatus; review_notes: string; reviewer: string }>({ review_status: "pending", review_notes: "", reviewer: "" });
const scores = ref<ExpertScore[]>(DIMENSIONS.map((item) => ({ dimension_key: item.key, score: null, comment: "" })));

const participant = computed(() => detail.value?.participant ?? {});
const naturalTraces = computed(() =>
  [...(detail.value?.traces ?? [])]
    .filter((trace) => ["natural_opening", "natural_interview_turn", "user_requested_finalize"].includes(trace.action ?? ""))
    .sort((a, b) => (a.turn_index ?? -1) - (b.turn_index ?? -1)),
);
const scoringRuns = computed(() =>
  [...(detail.value?.scoring_runs ?? [])].sort((a, b) => a.attempt_number - b.attempt_number),
);
const latestScoringError = computed(() =>
  [...(detail.value?.scoring_runs ?? [])]
    .sort((a, b) => b.attempt_number - a.attempt_number)
    .find((run) => run.error)?.error ?? "",
);
const canRetryReport = computed(() =>
  detail.value?.phase === "finalizing" && detail.value.report_available !== true,
);
const evidenceItems = computed<AdminEvidenceItem[]>(() => {
  if (detail.value?.evidence_items?.length) return detail.value.evidence_items;
  return (detail.value?.report?.dimensions ?? []).flatMap((dimension) =>
    dimension.evidences.map((evidence) => ({
      dimension_key: dimension.dimension_key,
      quote: evidence.quote,
      turn_index: evidence.turn_index,
      source_type: evidence.source_type,
      status: dimension.status,
    })),
  );
});
const inputStats = computed(() => {
  const userTurns = detail.value?.turns.filter((turn) => turn.role === "user") ?? [];
  const duration = userTurns.reduce((sum, turn) => sum + (turn.answer_duration_ms ?? 0), 0);
  return {
    text: userTurns.filter((turn) => turn.input_mode === "text").length,
    voice: userTurns.filter((turn) => turn.input_mode === "voice" || turn.input_mode === "voice_edited").length,
    duration,
  };
});

const phaseLabels: Record<string, string> = {
  interviewing: "访谈中",
  finalizing: "报告生成中",
  completed: "已完成",
  exited: "已退出",
  safety_stopped: "安全停止",
};

function nameFor(key?: string | null) {
  if (!key) return "未指定";
  return DIMENSIONS.find((item) => item.key === key)?.name ?? key;
}

function phaseName(value?: string) {
  return value ? phaseLabels[value] ?? value : "—";
}

function sourceName(value?: string) {
  return value === "user" || !value ? "用户原话" : value;
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    detail.value = await getAdminSession(uuid);
    review.review_status = detail.value.review_status ?? "pending";
    review.review_notes = detail.value.review_notes ?? "";
    review.reviewer = detail.value.reviewer ?? auth.user.value?.display_name ?? auth.user.value?.username ?? "";
    const existing = new Map((detail.value.expert_scores ?? []).map((item) => [item.dimension_key, item]));
    scores.value = DIMENSIONS.map((item) => existing.get(item.key) ?? { dimension_key: item.key, score: null, comment: "" });
  } catch {
    error.value = "无法读取该会话的复核信息。";
  } finally {
    loading.value = false;
  }
}

async function saveReview() {
  saving.value = true;
  error.value = "";
  try {
    await updateReview(uuid, review);
    await load();
    message.value = "人工复核状态已保存。";
  } catch {
    error.value = "人工复核保存失败。";
  } finally {
    saving.value = false;
  }
}

async function saveScores() {
  if (!review.reviewer.trim()) {
    error.value = "请先填写复核人。";
    return;
  }
  if (!scores.value.some((item) => item.score !== null)) {
    error.value = "请至少填写一个可判断维度的专家分数。";
    return;
  }
  saving.value = true;
  error.value = "";
  try {
    await saveExpertScores(uuid, { scores: scores.value, reviewer: review.reviewer.trim() });
    await load();
    message.value = "专家评分已保存，并与 AI 结果分开记录。";
  } catch {
    error.value = "专家评分保存失败。";
  } finally {
    saving.value = false;
  }
}

async function retryReport() {
  if (!canRetryReport.value || reportRetrying.value) return;
  reportRetrying.value = true;
  error.value = "";
  message.value = "正在使用已冻结的逐字稿重试生成报告…";
  try {
    const result = await finalizeAdminSession(uuid);
    await load();
    message.value = result.session.phase === "completed" || result.report
      ? "报告已生成，访谈逐字稿未重复写入。"
      : "会话仍在生成报告，可以稍后再次重试。";
  } catch (cause) {
    const failureMessage = cause instanceof Error ? cause.message : "报告生成失败，请查看最近一次终评错误后重试。";
    await load();
    error.value = failureMessage;
  } finally {
    reportRetrying.value = false;
  }
}

function formatMs(value?: number | null) {
  if (value === null || value === undefined) return "未记录";
  return value >= 60_000 ? `${(value / 60_000).toFixed(1)} 分钟` : `${Math.round(value / 1000)} 秒`;
}

function anomalyText(item: NonNullable<AdminSessionDetail["technical_anomalies"]>[number]) {
  return typeof item === "string" ? item : `${item.category}：${item.detail}`;
}

function anomalyTurn(item: NonNullable<AdminSessionDetail["technical_anomalies"]>[number]) {
  return typeof item === "string" ? null : item.turn_index;
}

function scoringDimensions(run: NonNullable<AdminSessionDetail["scoring_runs"]>[number]) {
  if (!run.result_data || typeof run.result_data !== "object") return [];
  const dimensions = (run.result_data as { dimensions?: unknown }).dimensions;
  if (!Array.isArray(dimensions)) return [];
  return dimensions.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const value = item as { dimension_key?: unknown; confidence?: unknown; score?: unknown; sufficient?: unknown };
    if (typeof value.dimension_key !== "string") return [];
    return [{
      dimension_key: value.dimension_key,
      confidence: typeof value.confidence === "number" ? value.confidence : null,
      score: typeof value.score === "number" ? value.score : null,
      sufficient: value.sufficient === true,
    }];
  });
}

onMounted(load);
</script>

<template>
  <main class="admin-page detail-page">
    <header class="admin-header"><RouterLink class="back-link" to="/admin/sessions">← 返回会话列表</RouterLink><span class="session-code">{{ uuid }}</span></header>
    <section v-if="loading" class="center-state">正在读取记录…</section>
    <section v-else-if="detail" class="admin-shell">
      <div class="detail-title">
        <div><span class="eyebrow">V6 NATURAL INTERVIEW REVIEW</span><h1>{{ participant.display_name || "匿名参与者" }}的自然访谈</h1><p>{{ phaseName(detail.phase) }} · {{ answerCount(detail) }} 次已保存回答</p></div>
        <div class="detail-title-actions">
          <div class="detail-stats"><span><strong>{{ inputStats.text }}</strong>文字输入</span><span><strong>{{ inputStats.voice }}</strong>语音输入</span><span><strong>{{ formatMs(inputStats.duration) }}</strong>累计作答</span></div>
          <button v-if="canRetryReport" type="button" class="primary-button small" :disabled="reportRetrying" @click="retryReport">{{ reportRetrying ? "正在生成…" : "生成/重试报告" }}</button>
        </div>
      </div>

      <nav class="tab-bar" aria-label="复核内容">
        <button v-for="tab in [{key:'conversation',label:'完整对话'},{key:'interview',label:'访谈与证据'},{key:'trace',label:'模型与评分'},{key:'review',label:'人工复核'}]" :key="tab.key" :class="{ active: activeTab === tab.key }" @click="activeTab = tab.key as ReviewTab">{{ tab.label }}</button>
      </nav>
      <p v-if="message" class="notice-banner">{{ message }}</p><p v-if="error" class="error-banner">{{ error }}</p>
      <p v-if="latestScoringError" class="error-banner">最近一次终评错误：{{ latestScoringError }}</p>

      <section v-if="activeTab === 'conversation'" class="review-conversation">
        <article v-for="turn in detail.turns" :key="turn.id ?? `${turn.turn_index}-${turn.role}`" :class="turn.role">
          <div><strong>#{{ turn.turn_index }} · {{ turn.role === "assistant" ? "澄澄" : "参与者" }}</strong></div>
          <p>{{ turn.content }}</p>
          <footer v-if="turn.role === 'user'"><span>{{ turn.input_mode || "未记录输入方式" }}</span><span>{{ formatMs(turn.answer_duration_ms) }}</span><code v-if="turn.client_turn_id">{{ turn.client_turn_id }}</code></footer>
        </article>
      </section>

      <section v-else-if="activeTab === 'interview'" class="evidence-review">
        <article class="natural-audit-card">
          <div class="section-heading"><div><span class="eyebrow">NATURAL INTERVIEW AUDIT</span><h2>访谈运行记录</h2></div><p>这里只记录模型实际的结束选择、质量标记与硬约束；不展示题库、目标维度或后台选题。</p></div>
          <div v-for="trace in naturalTraces" :key="trace.id ?? `${trace.action}-${trace.turn_index}`" class="natural-audit-row">
            <strong>{{ trace.turn_index === undefined ? '开场 / 用户结束' : `第 ${trace.turn_index} 次回答后` }}</strong>
            <span>动作：{{ trace.output_contract?.session_action === 'finish' ? '结束' : '继续' }}</span>
            <span>原因：{{ trace.output_contract?.finish_reason || '—' }}</span>
            <span>质量标记：{{ trace.output_contract?.quality_flags?.join('；') || '无' }}</span>
            <span>运行记录：{{ trace.action || '—' }}</span>
            <small>{{ trace.model || '—' }} · {{ trace.prompt_template_id || '—' }} / {{ trace.prompt_version || '—' }} · {{ trace.repair_used ? '已修复' : '未修复' }} · {{ trace.latency_ms ? `${trace.latency_ms} ms` : '—' }}</small>
          </div>
          <p v-if="!naturalTraces.length" class="empty-cell">暂无自然访谈运行记录</p>
        </article>

        <article class="evidence-table-card">
          <div class="section-heading"><div><span class="eyebrow">FINAL SCORING EVIDENCE</span><h2>终评引用的用户原话</h2></div><p>数字分数只应建立在与用户逐字稿精确匹配、且被判定为充分的证据之上。</p></div>
          <table class="admin-table"><thead><tr><th>回答</th><th>观察角度</th><th>来源</th><th>证据状态</th><th>用户原话</th></tr></thead><tbody><tr v-for="(item,index) in evidenceItems" :key="`${item.turn_index}-${index}`"><td>#{{ item.turn_index ?? '—' }}</td><td>{{ nameFor(item.dimension_key) }}</td><td>{{ sourceName(item.source_type) }}</td><td>{{ item.status || '—' }}</td><td>“{{ item.quote || '—' }}”</td></tr><tr v-if="!evidenceItems.length"><td colspan="5" class="empty-cell">暂无终评引用证据</td></tr></tbody></table>
        </article>
      </section>

      <section v-else-if="activeTab === 'trace'" class="trace-stack">
        <div v-if="detail.technical_anomalies?.length" class="anomaly-card"><strong>逐轮技术异常</strong><ul><li v-for="(item, index) in detail.technical_anomalies" :key="index"><span v-if="anomalyTurn(item) !== null">第 {{ anomalyTurn(item) }} 轮 · </span>{{ anomalyText(item) }}</li></ul></div>
        <article class="trace-review">
          <h2 class="audit-heading">访谈模型调用</h2>
          <table class="admin-table"><thead><tr><th>回答</th><th>模块 / 动作</th><th>模型</th><th>Prompt</th><th>修复 / 回退</th><th>耗时</th></tr></thead><tbody><tr v-for="trace in detail.traces || []" :key="trace.id"><td>#{{ trace.turn_index ?? '—' }}</td><td>{{ trace.module || '—' }}<small>{{ trace.action || '' }}</small></td><td>{{ trace.model || '—' }}</td><td><code>{{ trace.prompt_template_id || '—' }} / {{ trace.prompt_version || '—' }}</code></td><td><span :class="trace.renderer_status === 'failed' || trace.fallback_used || trace.repair_used ? 'fallback-yes' : 'fallback-no'">{{ trace.renderer_status === 'failed' ? trace.fallback_reason || '调用失败，可恢复' : trace.fallback_used ? trace.fallback_reason || '已回退' : trace.repair_used ? '已修复' : '否' }}</span></td><td>{{ trace.latency_ms ? `${trace.latency_ms} ms` : '—' }}</td></tr><tr v-if="!detail.traces?.length"><td colspan="6" class="empty-cell">暂无调用轨迹</td></tr></tbody></table>
        </article>
        <article class="trace-review scoring-run-card">
          <h2 class="audit-heading">独立终评运行</h2>
          <table class="admin-table"><thead><tr><th>尝试</th><th>状态</th><th>逐字稿指纹</th><th>模型 / Prompt</th><th>每项模型置信度</th><th>格式修复</th><th>人工复核</th><th>错误</th></tr></thead><tbody><tr v-for="run in scoringRuns" :key="run.id ?? run.attempt_number"><td>#{{ run.attempt_number }}</td><td><span class="table-badge">{{ run.status }}</span></td><td><code>{{ run.transcript_fingerprint || '—' }}</code></td><td>{{ run.model || '—' }}<small><code>{{ run.prompt_template_id || '—' }} / {{ run.prompt_version || '—' }}</code></small></td><td><ul v-if="scoringDimensions(run).length" class="confidence-list"><li v-for="item in scoringDimensions(run)" :key="item.dimension_key"><span>{{ nameFor(item.dimension_key) }}</span><b>{{ item.confidence === null ? '—' : item.confidence.toFixed(2) }}</b><small>未校准 · {{ item.score === null ? '未建议分数' : `模型原始建议 ${item.score} 分` }}</small></li></ul><span v-else>—</span></td><td>{{ run.repair_used ? '是' : '否' }}</td><td>{{ run.manual_review_recommended ? '建议' : '否' }}</td><td class="run-error">{{ run.error || '—' }}</td></tr><tr v-if="!scoringRuns.length"><td colspan="8" class="empty-cell">暂无独立终评运行</td></tr></tbody></table>
          <p class="audit-note">以上是终评器的原始快照。数字置信度未经校准，任何数字分数都必须有精确的用户原话和充分证据才能进入用户报告。</p>
        </article>
      </section>

      <section v-else class="review-forms">
        <form class="review-form" @submit.prevent="saveReview">
          <span class="eyebrow">HUMAN REVIEW</span><h2>人工复核结论</h2>
          <label><span>复核人</span><input v-model="review.reviewer" required placeholder="姓名或工号" /></label>
          <label><span>复核状态</span><select v-model="review.review_status"><option value="pending">待复核</option><option value="in_review">复核中</option><option value="approved">已确认</option><option value="needs_followup">需跟进</option></select></label>
          <label><span>复核备注</span><textarea v-model="review.review_notes" rows="6" placeholder="记录证据链、异常、分歧或后续动作" /></label>
          <button class="primary-button" :disabled="saving">保存复核</button>
        </form>
        <form class="review-form expert-form" @submit.prevent="saveScores">
          <span class="eyebrow">EXPERT SCORES</span><h2>专家独立评分</h2><p class="muted">与 AI 报告分开保存。无法判断时保留为空，不用 0 代替。</p>
          <div v-for="score in scores" :key="score.dimension_key" class="expert-row"><strong>{{ nameFor(score.dimension_key) }}</strong><select v-model="score.score"><option :value="null">不评分</option><option v-for="value in 5" :key="value" :value="value">{{ value }} 分</option></select><input v-model="score.comment" placeholder="评分依据或分歧说明" /></div>
          <button class="secondary-button" :disabled="saving">保存专家评分</button>
        </form>
      </section>
    </section>
    <section v-else class="center-state error-state">{{ error }}</section>
  </main>
</template>
