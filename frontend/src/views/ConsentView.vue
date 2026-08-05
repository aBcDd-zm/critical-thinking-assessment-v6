<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { createSession } from "@/api/session";
import { ApiError } from "@/api/http";

const router = useRouter();
const form = reactive({ display_name: "", consent: false });
const submitting = ref(false);
const error = ref("");
const recentSession = localStorage.getItem("v6:last-session");
const configuredResearchContact = (import.meta.env.VITE_RESEARCH_CONTACT || "").trim();
const configuredRetentionNotice = (import.meta.env.VITE_DATA_RETENTION_NOTICE || "").trim();
const isLocalMode = import.meta.env.DEV || import.meta.env.MODE === "test";
const researchContact = configuredResearchContact || (isLocalMode ? "本地受控测试：请联系当前测试组织者；不得用于正式招募" : "");
const retentionNotice = configuredRetentionNotice || (isLocalMode ? "本地受控测试数据应由测试组织者在本轮验收结束后清理；不得用于正式研究" : "");
const researchNoticeReady = Boolean(researchContact && retentionNotice);
const canSubmit = computed(() => researchNoticeReady && form.consent && Boolean(form.display_name.trim()));

async function startInterview() {
  if (!canSubmit.value || submitting.value) return;
  submitting.value = true;
  error.value = "";
  try {
    const response = await createSession({
      consent_version: "v6-research-pilot-2026-08",
      consent_given: true,
      participant: { display_name: form.display_name.trim() || undefined },
    });
    localStorage.setItem("v6:last-session", response.session.uuid);
    await router.push(`/assessment/session/${response.session.uuid}`);
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "暂时无法创建访谈，请确认本地服务已启动。";
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <main class="consent-page">
    <section class="consent-card" aria-labelledby="consent-title">
      <a class="brand" href="/assessment" aria-label="思衡首页"><span>思衡</span></a>
      <h1 id="consent-title">开始访谈</h1>
      <form @submit.prevent="startInterview">
        <label>
          <span>用户名</span>
          <input v-model="form.display_name" autocomplete="username" maxlength="40" placeholder="用于后续统计，请勿使用真实姓名" required />
        </label>
        <details class="consent-details" open>
          <summary>隐私与使用说明</summary>
          <p><strong>这是探索性、非标准化的研究试点。</strong>它不是标准化心理测验或临床诊断，结果不能替代专业决定。</p>
          <p>请使用用户名，不要填写真实姓名、单位、联系方式或其他可识别个人的信息。系统会保存你主动提交的用户名与文字回答、完整逐字稿、模型回应、会话状态、终评结果和必要的技术记录，用于恢复会话、生成报告和质量复核。</p>
          <p>访谈进行时，截至当时已保存的逐字稿会发送给已配置的第三方模型服务以生成下一轮回应；访谈结束后，冻结的完整逐字稿会再发送给该服务以生成终评和报告。</p>
          <p>只有获授权的研究人员可用这些信息进行报告复核和质量改进；用于比赛、论文或研究分析时，应先按正式研究方案完成去标识化。</p>
          <p>你可以随时结束或退出；退出前已提交的内容可能已被保存。数据保存与处置说明：<strong>{{ retentionNotice }}</strong></p>
          <p>如需询问、撤回或申请删除已保存数据，请按正式招募材料提供的渠道联系：<strong>{{ researchContact }}</strong></p>
        </details>
        <p v-if="!researchNoticeReady" class="error-banner" role="alert">研究联系渠道或数据保存与处置说明尚未配置，当前不能开始正式访谈。请联系部署负责人。</p>
        <label class="consent-check">
          <input v-model="form.consent" type="checkbox" :disabled="!researchNoticeReady" />
          <span>我已阅读并同意以上说明，自愿参加本次访谈。</span>
        </label>
        <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
        <button class="primary-button" type="submit" :disabled="!canSubmit || submitting">
          {{ submitting ? "正在开始…" : "开始访谈" }}
          <span aria-hidden="true">→</span>
        </button>
        <RouterLink v-if="recentSession" class="resume-link" :to="`/assessment/session/${recentSession}`">继续上次未完成的访谈</RouterLink>
      </form>
    </section>
  </main>
</template>
