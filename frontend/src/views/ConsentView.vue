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
const CONSENT_VERSION = "v6-natural-interview-guidance-2026-08";

async function startInterview() {
  if (!canSubmit.value || submitting.value) return;
  submitting.value = true;
  error.value = "";
  try {
    const response = await createSession({
      consent_version: CONSENT_VERSION,
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
      <header class="consent-intro">
        <h1 id="consent-title">在访谈对话中展现你的思维</h1>
        <p>请从一件真实、具体、需要判断的经历说起</p>
      </header>

      <aside v-if="recentSession" class="resume-banner" aria-labelledby="resume-title">
        <div>
          <strong id="resume-title">检测到一段未完成的访谈</strong>
          <span>可继续已保存的对话，也可在下方开始新访谈。</span>
        </div>
        <RouterLink class="secondary-button small" :to="`/assessment/session/${recentSession}`">继续上次访谈</RouterLink>
      </aside>

      <section class="assessment-guide" aria-labelledby="guide-title">
        <h2 id="guide-title">开始前请了解</h2>
        <ul class="process-facts">
          <li>没有标准答案，也不需要专业术语；请按真实想法回答。</li>
          <li>首答可以简短；第二次起，普通回答至少需要 20 个非空白字符。不知道或没想好时可直接说明。</li>
          <li>没有固定题单，AI 每次只会询问一个主要问题；至少完成 8 轮回答且现有证据足以支持完整报告时，系统才会提示可以结束，是否生成报告由你确认。</li>
        </ul>
        <p class="evidence-boundary">报告只依据可核对原话；证据不足会标注「证据有限」或「未充分测得」，不代表能力不足。本工具是探索性、非标准化访谈，不用于诊断、人格或智力判断、跨人排名或替代专业决定。</p>
      </section>

      <form :aria-busy="submitting" @submit.prevent="startInterview">
        <label for="participant-alias">
          <span>参与编号或昵称</span>
          <input
            id="participant-alias"
            v-model="form.display_name"
            aria-describedby="participant-alias-help"
            autocomplete="off"
            maxlength="40"
            placeholder="请勿填写真实姓名"
            required
            :disabled="submitting"
          />
          <small id="participant-alias-help" class="field-help">请勿填写真实姓名、单位、联系方式或其他可识别个人的信息。</small>
        </label>
        <p class="privacy-summary"><strong>隐私摘要：</strong>你的昵称和逐字稿会被保存，并发送给已配置的模型服务，用于生成回应和报告。退出前已提交的内容仍可能保留。</p>
        <details class="consent-details">
          <summary>查看完整的隐私与使用说明</summary>
          <p>访谈进行时，截至当时已保存的逐字稿与参与昵称会发送给已配置的模型服务，用于生成下一轮回应；访谈结束后，完整逐字稿会再用于生成报告。</p>
          <p>系统会保存你主动提交的内容、模型回应、会话状态、报告与必要的技术记录，用于恢复会话和生成报告；获授权的管理人员可能查看完整逐字稿以进行质量复核。</p>
          <p>你可以主动结束并生成报告，也可以退出且不生成报告；退出不会撤回已经保存或发送的内容。</p>
        </details>
        <label class="consent-check">
          <input v-model="form.consent" type="checkbox" required :disabled="submitting" />
          <span>我已阅读并理解访谈说明、数据处理方式和结果边界，自愿参加。</span>
        </label>
        <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
        <button class="primary-button" type="submit" :disabled="!canSubmit || submitting">
          {{ submitting ? "正在开始…" : "我已了解，开始访谈" }}
          <span aria-hidden="true">→</span>
        </button>
      </form>
    </section>
  </main>
</template>
