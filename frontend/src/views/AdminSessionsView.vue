<script setup lang="ts">
import { onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { downloadBlob } from "@/api/http";
import { getAnonymousExport, importExpertScores, listAdminSessions } from "@/api/admin";
import type { AdminSessionSummary } from "@/types/contracts";

const filters = reactive({ phase: "", review_status: "", manual_review_recommended: "", q: "" });
const sessions = ref<AdminSessionSummary[]>([]);
const total = ref(0);
const loading = ref(false);
const busy = ref(false);
const message = ref("");
const error = ref("");
const fileInput = ref<HTMLInputElement | null>(null);
const route = useRoute();
const router = useRouter();
const submissionScreenshotMode = import.meta.env.VITE_SUBMISSION_SCREENSHOT_MODE === "true";

function participantLabel(displayName?: string | null) {
  if (submissionScreenshotMode) return "[已去标识]";
  return displayName || "匿名参与者";
}

const phaseLabel: Record<string, string> = {
  interviewing: "访谈中",
  finalizing: "报告生成中",
  completed: "已完成",
  exited: "已退出",
  safety_stopped: "安全停止",
};
const reviewLabel: Record<string, string> = {
  pending: "待复核",
  in_review: "复核中",
  approved: "已确认",
  needs_followup: "需跟进",
};

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const result = await listAdminSessions(filters);
    sessions.value = result.items;
    total.value = result.total;
  } catch {
    error.value = "无法读取会话列表，请确认后端服务已启动。";
  } finally {
    loading.value = false;
  }
}

function applyRouteFilters() {
  filters.phase = typeof route.query.phase === "string" ? route.query.phase : "";
  filters.review_status = typeof route.query.review_status === "string" ? route.query.review_status : "";
  filters.manual_review_recommended = typeof route.query.manual_review_recommended === "string" ? route.query.manual_review_recommended : "";
  filters.q = typeof route.query.q === "string" ? route.query.q : "";
}

async function submitFilters() {
  const query = Object.fromEntries(Object.entries(filters).filter(([, value]) => Boolean(value)));
  // Route-driven quick filters (from the overview) and this form share one
  // source of truth.  A changed URL is loaded by the watcher; submitting the
  // unchanged URL still refreshes the list explicitly.
  if (router.resolve({ query }).fullPath === route.fullPath) {
    await load();
    return;
  }
  await router.replace({ query });
}

async function exportAnonymous() {
  busy.value = true;
  try {
    downloadBlob(await getAnonymousExport(), "siheng-anonymous-export.zip");
    message.value = "匿名数据已导出；默认不包含自由文本、原话或实际谈论内容。";
  } catch {
    error.value = "匿名导出失败。";
  } finally {
    busy.value = false;
  }
}

async function importCsv(event: Event) {
  const target = event.target as HTMLInputElement;
  const file = target.files?.[0];
  if (!file) return;
  busy.value = true;
  try {
    const result = await importExpertScores(file);
    message.value = `已导入 ${result.imported} 条专家评分${result.errors?.length ? `，${result.errors.length} 条需检查` : ""}。`;
    await load();
  } catch {
    error.value = "CSV 导入失败，请检查列名和会话编号。";
  } finally {
    busy.value = false;
    target.value = "";
  }
}

function formatDate(value?: string) {
  return value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—";
}

watch(() => route.fullPath, async () => {
  applyRouteFilters();
  await load();
});

onMounted(() => {
  applyRouteFilters();
  void load();
});
</script>

<template>
  <main class="admin-page">
    <header class="admin-header">
      <a class="brand compact" href="/admin"><span>思衡</span><small>复核台</small></a>
      <div><span class="local-trust-badge">仅限本地可信环境</span><RouterLink class="quiet-link" to="/assessment">用户端</RouterLink><button class="secondary-button small" :disabled="busy" @click="exportAnonymous">匿名导出</button></div>
    </header>
    <section class="admin-shell">
      <div class="admin-title"><div><h1>自然访谈与证据复核</h1><p>完整对话只在本地可信复核环境中查看；匿名导出默认排除自由文本。</p></div><div class="admin-title-actions"><button class="secondary-button" :disabled="busy" @click="exportAnonymous">匿名导出</button><button class="secondary-button" :disabled="busy" @click="fileInput?.click()">导入专家评分 CSV</button><input ref="fileInput" type="file" accept=".csv,text/csv" hidden @change="importCsv" /></div></div>

      <form class="filter-bar" @submit.prevent="submitFilters">
        <label><span>搜索</span><input v-model="filters.q" placeholder="会话编号或参与者称呼" /></label>
        <label><span>状态</span><select v-model="filters.phase"><option value="">全部</option><option v-for="(label, key) in phaseLabel" :key="key" :value="key">{{ label }}</option></select></label>
        <label><span>复核状态</span><select v-model="filters.review_status"><option value="">全部</option><option v-for="(label, key) in reviewLabel" :key="key" :value="key">{{ label }}</option></select></label>
        <label><span>人工复核建议</span><select v-model="filters.manual_review_recommended"><option value="">全部</option><option value="true">仅建议复核</option><option value="false">仅无建议</option></select></label>
        <button class="primary-button small" type="submit">筛选</button>
      </form>

      <p v-if="message" class="notice-banner">{{ message }}</p><p v-if="error" class="error-banner">{{ error }}</p>
      <div class="table-meta"><span>共 {{ total }} 个会话</span><button class="quiet-button" @click="load">刷新</button></div>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead><tr><th>会话</th><th>状态</th><th>已保存回答</th><th>建议人工复核</th><th>复核状态</th><th>更新时间</th><th /></tr></thead>
          <tbody>
            <tr v-if="loading"><td colspan="7" class="empty-cell">正在读取…</td></tr>
            <tr v-else-if="!sessions.length"><td colspan="7" class="empty-cell">暂无符合条件的会话</td></tr>
            <tr v-for="item in sessions" v-else :key="item.uuid">
              <td><code>{{ item.uuid.slice(0, 8) }}</code><small>{{ participantLabel(item.display_name) }}</small></td>
              <td><span class="table-badge">{{ phaseLabel[item.phase] || item.phase }}</span></td>
              <td>{{ item.user_answer_count ?? 0 }}</td>
              <td><span :class="item.manual_review_recommended ? 'fallback-yes' : 'fallback-no'">{{ item.manual_review_recommended ? "是" : "否" }}</span></td>
              <td><span class="review-badge" :class="item.review_status || 'pending'">{{ reviewLabel[item.review_status || "pending"] }}</span></td>
              <td>{{ formatDate(item.updated_at || item.created_at) }}</td>
              <td><RouterLink class="row-link" :to="`/admin/sessions/${item.uuid}`">打开复核 →</RouterLink></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>
</template>
