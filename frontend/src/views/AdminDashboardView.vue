<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { getDashboardOverview } from "@/api/admin";
import type { DashboardOverview, SessionPhase } from "@/types/contracts";
import { formatBeijingDateTime } from "@/utils/dateTime";

const data = ref<DashboardOverview | null>(null);
const loading = ref(true);
const error = ref("");

const phaseLabels: Record<SessionPhase, string> = {
  interviewing: "访谈中",
  finalizing: "报告生成中",
  completed: "已完成",
  exited: "已退出",
  safety_stopped: "安全停止",
};

const phaseRows = computed(() => Object.entries(data.value?.measurement.phase_counts || {})
  .map(([phase, count]) => ({ phase: phase as SessionPhase, count }))
  .filter((item) => item.count > 0));

function percent(value: number) {
  return `${Math.round(value * 100)}%`;
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    data.value = await getDashboardOverview();
  } catch {
    error.value = "无法读取复核概览，请刷新或重新登录。";
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section class="console-page dashboard-page">
    <div class="console-page-heading"><div><span class="eyebrow">REVIEW OVERVIEW</span><h2>复核概览</h2><p>汇总 V6 自然访谈的运行状态、复核待办与链路记录，不呈现能力排名。</p></div><button class="secondary-button small" :disabled="loading" @click="load">刷新数据</button></div>
    <p v-if="error" class="error-banner">{{ error }}</p>
    <section v-else-if="loading" class="console-loading"><span class="loading-ring" />正在汇总复核数据…</section>
    <template v-else-if="data">
      <div class="metric-grid primary-metrics">
        <article><span>总会话</span><strong>{{ data.measurement.total_sessions }}</strong><small>所有已保存会话</small></article>
        <article><span>已完成</span><strong>{{ data.measurement.completed_sessions }}</strong><small>完成率 {{ percent(data.measurement.completion_rate) }}</small></article>
        <article><span>进行中</span><strong>{{ data.measurement.active_sessions }}</strong><small>访谈或报告处理中</small></article>
        <RouterLink class="metric-card-link priority" :to="{ name: 'admin-sessions', query: { manual_review_recommended: 'true' } }"><span>优先复核</span><strong>{{ data.review_queue.manual_review_recommended }}</strong><small>系统建议人工查看 →</small></RouterLink>
      </div>
      <div class="dashboard-columns">
        <section class="console-panel phase-panel"><div class="panel-heading"><div><span class="eyebrow">SESSION FLOW</span><h3>会话状态</h3></div><RouterLink to="/admin/sessions">查看全部 →</RouterLink></div><div v-if="phaseRows.length" class="phase-list"><div v-for="item in phaseRows" :key="item.phase"><span>{{ phaseLabels[item.phase] }}</span><b>{{ item.count }}</b><i :style="{ width: `${data.measurement.total_sessions ? (item.count / data.measurement.total_sessions) * 100 : 0}%` }" /></div></div><p v-else class="empty-copy">暂无会话数据。</p></section>
        <section class="console-panel"><div class="panel-heading"><div><span class="eyebrow">REVIEW QUEUE</span><h3>复核待办</h3></div><RouterLink :to="{ name: 'admin-sessions', query: { review_status: 'pending' } }">处理待办 →</RouterLink></div><div class="queue-list"><RouterLink :to="{ name: 'admin-sessions', query: { review_status: 'pending' } }"><span>待复核</span><b>{{ data.review_queue.pending }}</b></RouterLink><RouterLink :to="{ name: 'admin-sessions', query: { review_status: 'in_review' } }"><span>复核中</span><b>{{ data.review_queue.in_review }}</b></RouterLink><RouterLink :to="{ name: 'admin-sessions', query: { review_status: 'approved' } }"><span>已确认</span><b>{{ data.review_queue.approved }}</b></RouterLink><RouterLink :to="{ name: 'admin-sessions', query: { review_status: 'needs_followup' } }"><span>需跟进</span><b>{{ data.review_queue.needs_followup }}</b></RouterLink><span><span>已录入专家评分</span><b>{{ data.review_queue.expert_scored_sessions }}</b></span></div></section>
      </div>
      <section class="console-panel health-panel"><div class="panel-heading"><div><span class="eyebrow">PIPELINE HEALTH</span><h3>链路记录</h3></div><p>用于定位技术异常，不代表受测者能力。</p></div><div class="health-grid"><span><b>{{ data.pipeline_health.reports_generated }}</b>已生成报告</span><span><b>{{ data.pipeline_health.scoring_failures }}</b>终评失败</span><span><b>{{ data.pipeline_health.failed_traces }}</b>调用失败</span><span><b>{{ data.pipeline_health.repaired_traces }}</b>结构修复</span><span><b>{{ data.pipeline_health.technical_anomalies }}</b>技术异常</span></div></section>
      <section class="console-panel recent-panel"><div class="panel-heading"><div><span class="eyebrow">RECENT SESSIONS</span><h3>最近会话</h3></div><RouterLink to="/admin/sessions">进入会话复核 →</RouterLink></div><div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>会话</th><th>状态</th><th>回答</th><th>复核</th><th>更新时间（北京时间）</th><th /></tr></thead><tbody><tr v-if="!data.recent_sessions.length"><td colspan="6" class="empty-cell">暂无会话数据</td></tr><tr v-for="session in data.recent_sessions" :key="session.uuid"><td><code>{{ session.uuid.slice(0, 8) }}</code><small>{{ session.display_name || "匿名参与者" }}</small></td><td><span class="table-badge">{{ phaseLabels[session.phase] }}</span></td><td>{{ session.user_answer_count || 0 }}</td><td><span :class="session.manual_review_recommended ? 'fallback-yes' : 'fallback-no'">{{ session.manual_review_recommended ? "优先复核" : session.review_status || "待复核" }}</span></td><td>{{ formatBeijingDateTime(session.updated_at || session.created_at) }}</td><td><RouterLink class="row-link" :to="{ name: 'admin-session-detail', params: { sessionUuid: session.uuid } }">打开复核 →</RouterLink></td></tr></tbody></table></div></section>
    </template>
  </section>
</template>
