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
const canSubmit = computed(() => form.consent && Boolean(form.display_name.trim()));

async function startInterview() {
  if (!canSubmit.value || submitting.value) return;
  submitting.value = true;
  error.value = "";
  try {
    const response = await createSession({
      consent_version: "v6-natural-interview-2026-08",
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
        <div class="consent-guidance">
          <p>在访谈对话中展现你的思维</p>
          <p>请从一件真实、具体、需要判断的经历说起</p>
        </div>
        <label>
          <span>用户名</span>
          <input v-model="form.display_name" autocomplete="username" maxlength="40" placeholder="用于后续统计，请勿使用真实姓名" required />
        </label>
        <details class="consent-details">
          <summary>隐私与使用说明</summary>
          <p>请使用用户名，不要填写真实姓名、单位、联系方式或其他可识别个人的信息。你的文字会被保存，用于恢复会话和生成报告。</p>
          <p>当前访谈使用已配置的模型服务；提交后，回答与逐字稿会发送给该服务以生成回应和报告。</p>
          <p>这是实验性、非标准化访谈；你可以随时结束或退出，结果不能替代专业决定。</p>
        </details>
        <label class="consent-check">
          <input v-model="form.consent" type="checkbox" />
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
