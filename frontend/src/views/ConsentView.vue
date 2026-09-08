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
const screenshotMode = import.meta.env.VITE_SUBMISSION_SCREENSHOT_MODE === "true";
const canSubmit = computed(() => form.consent && Boolean(form.display_name.trim()));
const CONSENT_VERSION = "v6-natural-interview-guidance-2026-08-v2";

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
        <span class="eyebrow">批判性思维探索访谈</span>
        <h1 id="consent-title">开始一次具体的思维访谈</h1>
        <p>感谢你参与「思衡」。这不是与 AI 随意聊天：请从工作、学习、项目或生活中，选择一件亲身经历、需要判断、取舍或行动的具体事情，围绕同一件事说明经过、想法和理由。</p>
      </header>

      <aside v-if="recentSession && !screenshotMode" class="resume-banner" aria-labelledby="resume-title">
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
        <p class="privacy-summary"><strong>隐私摘要：</strong>你的昵称和逐字稿会被保存，并发送给 DeepSeek，用于生成回应和报告；只有在你主动使用语音输入或语音播报时，录音或 AI 生成文本才会发送给语音服务商豆包。退出前已提交的内容仍可能保留。</p>
        <details class="consent-details">
          <summary>查看完整的隐私与使用说明</summary>
          <p>访谈进行时，截至当时已保存的逐字稿与参与昵称会发送给已配置的模型服务，用于生成下一轮回应；访谈结束后，完整逐字稿会再用于生成报告。</p>
          <p>系统会保存你主动提交的内容、模型回应、会话状态、报告与必要的技术记录，用于恢复会话和生成报告；获授权的管理人员可能查看完整逐字稿以进行质量复核。</p>
          <p>你可以主动结束并生成报告，也可以退出且不生成报告；退出不会撤回已经保存或发送的内容。</p>
          <h3>四、模型服务与数据传输</h3>
          <p>系统的对话与评分由 DeepSeek 提供的模型能力支持，通过其官方 API 调用，因此你的对话内容会发送至 DeepSeek 服务。</p>
          <p>此外，系统提供可选的语音输入与语音播报功能。若你使用语音输入，录音将发送至第三方语音服务商豆包进行转写；若你使用语音播报，系统会将 AI 生成的文本发送至该服务合成语音。不使用语音功能时，不会有任何内容发送至该服务商。</p>
          <p>本项目未与上述任一供应商签署单独的数据处理协议，其对请求内容的保留、是否用于模型训练以及删除范围，均适用各自公开的服务条款与隐私政策，本项目无法代为承诺其服务端的删除范围与时限。</p>
          <h3>六、保留期限、退出与删除</h3>
          <p>删除范围涉及本项目数据库、分析文件与备份；模型服务商与语音服务商侧的删除以其各自条款为准。</p>
        </details>
        <label class="consent-check">
          <input v-model="form.consent" type="checkbox" required :disabled="submitting" />
          <span>我已阅读并理解本知情同意书，知晓 AI、DeepSeek 与语音服务商的数据处理方式、去标识化的边界、结果用途与禁止用途，并自愿参加。</span>
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
