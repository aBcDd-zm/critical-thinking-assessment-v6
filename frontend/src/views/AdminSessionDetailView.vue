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
  type EvidenceAttribution,
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
    .filter((trace) => [
      "natural_opening",
      "natural_interview_turn",
      "natural_close_suggested",
      "user_accepted_closure_suggestion",
      "user_requested_finalize",
    ].includes(trace.action ?? ""))
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
const evidenceAttributions = computed<EvidenceAttribution[]>(() => detail.value?.evidence_attributions ?? []);
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
  return value === "user" || !value ? "参与者输入" : value;
}

const ownerLabels: Record<EvidenceAttribution["owner"], string> = {
  participant_owned: "参与者本人",
  external_quoted: "外部材料（原文引用）",
  external_paraphrased: "外部材料（转述）",
  uncertain: "归属不确定",
};

const relationLabels: Record<EvidenceAttribution["relation"], string> = {
  own_reasoning: "本人推理",
  endorses: "采纳 / 赞同",
  critiques: "批评 / 质疑",
  rejects: "拒绝 / 反驳",
  quotes_only: "仅引用",
  asks_or_requests: "提问 / 请求",
};

const elicitationLabels: Record<EvidenceAttribution["elicitation_level"], string> = {
  spontaneous: "自发表达",
  open_probe: "开放追问",
  focused_probe: "聚焦追问",
  strong_scaffold: "强提示",
};

const eligibilityLabels = {
  eligible: "可作为评分候选",
  context_only: "仅作上下文",
  manual_review: "需人工复核",
} as const;

const validationLabels = {
  pending: "待校验",
  validated: "已校验",
  rejected: "已拒绝",
  manual_review: "需人工复核",
  legacy_unclassified: "旧数据未归类",
} as const;

function attributionId(item: EvidenceAttribution) {
  return item.span_id ?? item.id ?? "—";
}

function attributionEligibility(item: EvidenceAttribution) {
  const value = item.eligibility ?? item.eligibility_status;
  return value ? eligibilityLabels[value] : "未记录";
}

function attributionValidation(item: EvidenceAttribution) {
  return item.validation_status ? validationLabels[item.validation_status] : "未记录";
}

function dimensionNames(keys: string[]) {
  return keys.map((key) => nameFor(key)).join("、");
}

function hasFinalEvidenceReference(item: EvidenceAttribution) {
  const id = item.span_id ?? item.id;
  if (id === null || id === undefined) return false;
  return evidenceItems.value.some((evidence) =>
    evidence.attribution_span_id !== null
    && evidence.attribution_span_id !== undefined
    && String(evidence.attribution_span_id) === String(id)
    && evidence.active_for_scoring !== false,
  );
}

function attributionUsage(item: EvidenceAttribution) {
  const eligibility = item.eligibility ?? item.eligibility_status;
  if (item.validation_status === "rejected") return "未用于评分：归属校验已拒绝";
  if (eligibility === "context_only") return "未用于评分：仅作上下文";
  if (eligibility === "manual_review") return "未自动用于评分：需人工复核";

  const hasExplicitUsage = Array.isArray(item.final_scoring_dimension_keys)
    || Array.isArray(item.snapshot_used_dimension_keys);
  if (hasExplicitUsage) {
    const finalKeys = item.final_scoring_dimension_keys ?? [];
    const finalSet = new Set(finalKeys);
    const snapshotOnlyKeys = (item.snapshot_used_dimension_keys ?? []).filter((key) => !finalSet.has(key));
    const usage: string[] = [];
    if (finalKeys.length) usage.push(`终评已采用：${dimensionNames(finalKeys)}`);
    if (snapshotOnlyKeys.length) usage.push(`当前证据快照引用：${dimensionNames(snapshotOnlyKeys)}`);
    if (usage.length) return usage.join("；");
    if (eligibility === "eligible") return "候选片段未被终评采用";
    return "未记录评分用途";
  }

  // V6.2.1 rollout compatibility: old producers exposed one merged field.
  // A pre-final session must never make that field look like a completed score.
  if (item.used_dimension_keys?.length) {
    const finalized = detail.value?.phase === "completed"
      || detail.value?.report_available === true
      || Boolean(detail.value?.report)
      || hasFinalEvidenceReference(item);
    return finalized
      ? `终评已采用：${dimensionNames(item.used_dimension_keys)}`
      : `当前证据快照引用：${dimensionNames(item.used_dimension_keys)}`;
  }
  if (eligibility === "eligible") return "候选片段未被终评采用";
  return "未记录评分用途";
}

function attributionConfidence(value?: number | null) {
  return typeof value === "number" ? value.toFixed(2) : "—";
}

function evidenceValidationText(item: AdminEvidenceItem) {
  const status = item.validation_status ? validationLabels[item.validation_status] : "";
  return [status, item.validation_reason].filter(Boolean).join("：");
}

function evidenceStatusText(item: AdminEvidenceItem) {
  if (item.status) return item.status;
  return item.validation_status ? validationLabels[item.validation_status] : "—";
}

function evidencePositionText(item: AdminEvidenceItem) {
  const parts: string[] = [];
  if (typeof item.quote_start === "number" && typeof item.quote_end === "number") {
    parts.push(`字符 [${item.quote_start}, ${item.quote_end})`);
  }
  if (typeof item.confidence === "number") parts.push(`证据置信度 ${item.confidence.toFixed(2)}`);
  return parts.join(" · ");
}

function sessionActionName(value?: unknown) {
  if (value === "suggest_finish") return "建议结束";
  if (value === "finish") return "结束";
  if (value === "continue") return "继续";
  return "—";
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
    await load();
    error.value = cause instanceof Error ? cause.message : "报告生成失败，请稍后重试。";
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
        <div class="detail-stats"><span><strong>{{ inputStats.text }}</strong>文字输入</span><span><strong>{{ inputStats.voice }}</strong>语音输入</span><span><strong>{{ formatMs(inputStats.duration) }}</strong>累计作答</span><button v-if="canRetryReport" type="button" class="primary-button small" :disabled="reportRetrying" @click="retryReport">{{ reportRetrying ? "正在生成…" : "生成/重试报告" }}</button></div>
      </div>

      <nav class="tab-bar" aria-label="复核内容">
        <button v-for="tab in [{key:'conversation',label:'完整对话'},{key:'interview',label:'访谈与证据'},{key:'trace',label:'模型与评分'},{key:'review',label:'人工复核'}]" :key="tab.key" :class="{ active: activeTab === tab.key }" @click="activeTab = tab.key as ReviewTab">{{ tab.label }}</button>
      </nav>
      <p v-if="message" class="notice-banner">{{ message }}</p><p v-if="error" class="error-banner">{{ error }}</p><p v-if="latestScoringError" class="error-banner">最近一次终评错误：{{ latestScoringError }}</p>

      <section v-if="activeTab === 'conversation'" class="review-conversation">
        <article v-for="turn in detail.turns" :key="turn.id ?? `${turn.turn_index}-${turn.role}`" :class="turn.role">
          <div><strong>#{{ turn.turn_index }} · {{ turn.role === "assistant" ? "澄澄" : "参与者" }}</strong></div>
          <p>{{ turn.content }}</p>
          <footer v-if="turn.role === 'user'"><span>{{ turn.input_mode || "未记录输入方式" }}</span><span>{{ formatMs(turn.answer_duration_ms) }}</span><code v-if="turn.client_turn_id">{{ turn.client_turn_id }}</code></footer>
        </article>
      </section>

      <section v-else-if="activeTab === 'interview'" class="evidence-review">
        <article class="natural-audit-card">
          <div class="section-heading"><div><span class="eyebrow">NATURAL INTERVIEW AUDIT</span><h2>访谈运行记录</h2></div><p>这里记录模型实际的继续或结束建议、参与者确认与质量标记；不展示题库、目标维度或后台选题。</p></div>
          <div v-for="trace in naturalTraces" :key="trace.id ?? `${trace.action}-${trace.turn_index}`" class="natural-audit-row">
            <strong>{{ trace.turn_index === undefined ? '开场 / 用户结束' : `第 ${trace.turn_index} 次回答后` }}</strong>
            <span>动作：{{ sessionActionName(trace.output_contract?.session_action) }}</span>
            <span>原因：{{ trace.output_contract?.finish_reason || '—' }}</span>
            <span>质量标记：{{ trace.output_contract?.quality_flags?.join('；') || '无' }}</span>
            <span>运行记录：{{ trace.action || '—' }}</span>
            <small>{{ trace.model || '—' }} · {{ trace.prompt_template_id || '—' }} / {{ trace.prompt_version || '—' }} · {{ trace.repair_used ? '已修复' : '未修复' }} · {{ trace.latency_ms ? `${trace.latency_ms} ms` : '—' }}</small>
          </div>
          <p v-if="!naturalTraces.length" class="empty-cell">暂无自然访谈运行记录</p>
        </article>

        <article class="attribution-card">
          <div class="section-heading">
            <div><span class="eyebrow">EVIDENCE ATTRIBUTION</span><h2>输入片段 / 归属</h2></div>
            <p>独立归属层会区分参与者本人的推理、外部材料与归属不确定内容；只有服务端判定合格的片段才能进入数字评分。</p>
          </div>
          <div v-if="evidenceAttributions.length" class="attribution-list">
            <section
              v-for="item in evidenceAttributions"
              :key="String(attributionId(item))"
              class="attribution-row"
              :class="[`owner-${item.owner}`, { 'attribution-rejected': item.validation_status === 'rejected' }]"
            >
              <header>
                <strong>第 {{ item.turn_index }} 次回答 · span #{{ attributionId(item) }}</strong>
                <span class="attribution-badge" :class="item.owner">{{ ownerLabels[item.owner] }}</span>
                <span class="attribution-badge relation">{{ relationLabels[item.relation] }}</span>
              </header>
              <p class="eliciting-question"><strong>前一问</strong><span>{{ item.eliciting_question || '未记录' }}</span></p>
              <blockquote>
                <mark class="attribution-span" :class="item.owner">{{ item.quote || '—' }}</mark>
                <small>字符 [{{ item.start }}, {{ item.end }}) · 来源标签：{{ item.source_label || '未声明' }}</small>
              </blockquote>
              <dl class="attribution-meta">
                <div><dt>归属</dt><dd>{{ ownerLabels[item.owner] }}</dd></div>
                <div><dt>与外部内容的关系</dt><dd>{{ relationLabels[item.relation] }}</dd></div>
                <div><dt>提示强度</dt><dd>{{ elicitationLabels[item.elicitation_level] }}</dd></div>
                <div><dt>归属置信度</dt><dd>{{ attributionConfidence(item.confidence) }}</dd></div>
                <div><dt>服务端资格</dt><dd>{{ attributionEligibility(item) }}</dd></div>
                <div><dt>校验状态</dt><dd>{{ attributionValidation(item) }}</dd></div>
              </dl>
              <p class="attribution-usage"><strong>评分用途</strong><span>{{ attributionUsage(item) }}</span></p>
              <p v-if="item.validation_reason || item.reason" class="attribution-reason"><strong>{{ item.validation_status === 'rejected' ? '拒绝原因' : '判定原因' }}</strong><span>{{ item.validation_reason || item.reason }}</span></p>
            </section>
          </div>
          <p v-else class="empty-cell attribution-empty">暂无独立证据归属记录（旧合同或未启用）</p>
        </article>

        <article class="evidence-table-card">
          <div class="section-heading"><div><span class="eyebrow">FINAL SCORING EVIDENCE</span><h2>终评采用的输入片段 / 归属</h2></div><p>数字分数只应建立在已完成归属校验、可追溯到参与者推理的合格输入片段之上。</p></div>
          <table class="admin-table"><thead><tr><th>回答</th><th>观察角度</th><th>输入来源</th><th>证据状态</th><th>输入片段 / 归属校验</th></tr></thead><tbody><tr v-for="(item,index) in evidenceItems" :key="`${item.turn_index}-${index}`"><td>#{{ item.turn_index ?? '—' }}</td><td>{{ nameFor(item.dimension_key) }}</td><td>{{ sourceName(item.source_type) }}</td><td>{{ evidenceStatusText(item) }}<small v-if="item.active_for_scoring !== undefined">{{ item.active_for_scoring ? '用于终评' : '未用于终评' }}</small></td><td>“{{ item.quote || '—' }}”<small v-if="evidencePositionText(item)">{{ evidencePositionText(item) }}</small><small v-if="item.attribution_span_id !== null && item.attribution_span_id !== undefined">归属 span #{{ item.attribution_span_id }}</small><small v-if="evidenceValidationText(item)">{{ evidenceValidationText(item) }}</small></td></tr><tr v-if="!evidenceItems.length"><td colspan="5" class="empty-cell">暂无终评采用证据</td></tr></tbody></table>
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
          <p class="audit-note">以上是终评器的原始快照。数字置信度未经校准，任何数字分数都必须有精确的输入片段、明确归属和充分证据才能进入参与者报告。</p>
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
