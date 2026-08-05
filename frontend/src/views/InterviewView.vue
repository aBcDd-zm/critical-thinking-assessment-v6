<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { onBeforeRouteLeave, useRoute, useRouter } from "vue-router";
import InterviewerAvatar from "@/components/InterviewerAvatar.vue";
import { ApiError } from "@/api/http";
import {
  completedData,
  exitSession,
  finalizingSession,
  finalizeSession,
  getSession,
  submitTurnStream,
} from "@/api/session";
import { useSpeechPlayback, useVoiceInput } from "@/composables/useVoice";
import { answerCount } from "@/types/contracts";
import type {
  DialogueTurn,
  InputMode,
  InteractionKind,
  InterviewerState,
  SessionSnapshot,
  TurnRequest,
  TurnStreamEvent,
} from "@/types/contracts";

const route = useRoute();
const router = useRouter();
const uuid = computed(() => String(route.params.sessionUuid));
const session = ref<SessionSnapshot | null>(null);
const turns = ref<DialogueTurn[]>([]);
const draft = ref("");
const streamedText = ref("");
const loading = ref(true);
const sending = ref(false);
const finalizing = ref(false);
const error = ref("");
const notice = ref("");
const inputMode = ref<InputMode>("text");
const voiceWasUsed = ref(false);
const answerStartedAt = ref(Date.now());
const transcriptEnd = ref<HTMLElement | null>(null);
const ttsEnabled = ref(localStorage.getItem("v6:tts-enabled") !== "false");
const leaving = ref(false);
const playback = useSpeechPlayback();
let activeController: AbortController | null = null;

const MINIMUM_VALID_ANSWERS = 40;
const SUBSEQUENT_ANSWER_MIN_VISIBLE_CHARACTERS = 20;
const CLARIFICATION_REQUEST = "我没理解，请换一种问法。";
const IMMEDIATE_RISK_MARKERS = [
  "自杀", "自残", "自伤", "轻生", "寻死", "不想活", "活不下去", "我想死", "我要死",
  "结束生命", "割腕", "跳楼", "吞药", "杀了他", "杀了她", "杀人", "杀害他人", "伤害他人",
  "杀害别人", "伤害别人", "杀害其他人", "伤害其他人", "伤害自己", "我要杀人", "我想杀人",
  "伤害我自己", "我要伤害他", "我想伤害他", "我要伤害她", "我想伤害她", "我要伤害你", "我想伤害你", "我要伤害人", "我想伤害人",
  "捅人", "砍人", "开枪", "正在打我", "家暴", "被绑架",
  "suicide", "selfharm", "harmmyself", "hurtmyself", "killmyself", "iwanttodie", "endmylife",
  "overdose", "killsomeone", "harmsomeone", "hurtsomeone", "stabsomeone", "immediatedanger",
] as const;
const SAFETY_CONTEXT_EXEMPTIONS = [
  "自杀预防", "自伤预防", "suicideprevention", "selfharmprevention", "过去", "曾经", "历史上", "朋友说", "新闻里",
] as const;
const PRESENT_RISK_MARKERS = ["现在", "今晚", "马上", "准备", "计划", "rightnow", "tonight", "imgoingto"] as const;

function visibleCharacterCount(value: string): number {
  return Array.from(value.normalize("NFKC")).filter(
    (character) => /[\p{L}\p{N}]/u.test(character),
  ).length;
}

function normalizedIntentText(value: string): string {
  return value
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/殺/gu, "杀")
    .replace(/殘/gu, "残")
    .replace(/傷/gu, "伤")
    .replace(/輕/gu, "轻")
    .replace(/尋/gu, "寻")
    .replace(/藥/gu, "药")
    .replace(/綁/gu, "绑")
    .replace(/樓/gu, "楼")
    .replace(/開/gu, "开")
    .replace(/[і]/gu, "i")
    .replace(/[ѕ]/gu, "s")
    .replace(/[с]/gu, "c")
    .replace(/[е]/gu, "e")
    .replace(/[а]/gu, "a")
    .replace(/[о]/gu, "o")
    .replace(/[\s\p{P}]/gu, "");
}

function isClarificationText(value: string): boolean {
  const compact = Array.from(value.normalize("NFKC"))
    .filter((character) => /[\p{L}\p{N}]/u.test(character))
    .join("");
  if (!compact || visibleCharacterCount(compact) > 24) return false;
  return (
    /^(?:我)?(?:没|不)(?:听懂|理解|明白)(?:你?刚才(?:的)?(?:问题|意思|说法)?)?$/u.test(compact)
    || /^(?:这|刚才|你刚才说的)?什么意思$/u.test(compact)
    || /^(?:请|可以|能不能|麻烦)?(?:换(?:一个|一种|个|种)?(?:问法|说法)|再(?:说|解释)(?:一遍|一下)?)$/u.test(compact)
    || compact === "我没理解请换一种问法"
  );
}

function isSafetyText(value: string): boolean {
  const normalized = normalizedIntentText(value);
  if (!IMMEDIATE_RISK_MARKERS.some((marker) => normalized.includes(marker))) return false;
  if (SAFETY_CONTEXT_EXEMPTIONS.some((marker) => normalized.includes(marker))) {
    return PRESENT_RISK_MARKERS.some((marker) => normalized.includes(marker));
  }
  return true;
}

function isExitText(value: string): boolean {
  const normalized = value.normalize("NFKC").trim();
  if (!normalized) return false;
  const directRequests = [
    /^(?:我)?(?:现在)?(?:想|要|希望|决定)?(?:就|先)?(?:结束|停止|退出)(?:这次|本次)?(?:访谈|对话|聊天|交流)?(?:了|吧)?$/u,
    /^(?:我)?不想(?:再)?(?:继续|聊|回答|说)(?:了|下去)?$/u,
    /^(?:就)?到这里(?:就好|可以)?(?:了|吧)?$/u,
    /^(?:先)?这样(?:就好|可以)?(?:了|吧)?$/u,
    /^(?:不用|不要|别)(?:再)?(?:问|继续)(?:了|吧)?$/u,
    /^(?:现在)?(?:请)?(?:结束访谈|结束对话|停止访谈|停止对话|生成报告)(?:了|吧)?$/u,
  ];
  const clauses = [
    normalized,
    ...normalized.split(/[，,。.!！?？；;:：\n]+/u).filter((part) => part.trim()),
  ];
  return clauses
    .slice(-2)
    .map((clause) => clause.replace(/\s+/gu, ""))
    .some((clause) => directRequests.some((pattern) => pattern.test(clause)));
}

const interviewerState = computed<InterviewerState>(() => {
  if (voice.listening.value) return "listening";
  if (playback.speaking.value) return "speaking";
  if (sending.value || finalizing.value) return "thinking";
  return "listening";
});

const voice = useVoiceInput(
  (text) => {
    draft.value = text;
    inputMode.value = "voice";
    voiceWasUsed.value = true;
  },
  () => playback.stop(),
);

const isInterviewing = computed(() => session.value?.phase === "interviewing");
const validAnswerCount = computed(() => (
  session.value
    ? answerCount({ ...session.value, turns: turns.value })
    : 0
));
const minimumValidAnswers = computed(() => {
  const serverMinimum = session.value?.minimum_valid_answers;
  return typeof serverMinimum === "number" && Number.isFinite(serverMinimum) && serverMinimum > 0
    ? Math.floor(serverMinimum)
    : MINIMUM_VALID_ANSWERS;
});
const remainingRequiredAnswers = computed(() => {
  const serverRemaining = session.value?.remaining_required_answers;
  return typeof serverRemaining === "number" && Number.isFinite(serverRemaining)
    ? Math.max(0, Math.floor(serverRemaining))
    : Math.max(0, minimumValidAnswers.value - validAnswerCount.value);
});
const visibleDraftLength = computed(() => visibleCharacterCount(draft.value));
const isFirstValidAnswer = computed(() => validAnswerCount.value === 0);
const requiredVisibleCharacters = computed(() => (
  isFirstValidAnswer.value ? 1 : SUBSEQUENT_ANSWER_MIN_VISIBLE_CHARACTERS
));
const isShortAnswerExempt = computed(() => (
  isClarificationText(draft.value)
  || isSafetyText(draft.value)
  || isExitText(draft.value)
));
const meetsAnswerLength = computed(() => (
  visibleDraftLength.value >= requiredVisibleCharacters.value || isShortAnswerExempt.value
));
const remainingAnswerCharacters = computed(() => Math.max(
  0,
  requiredVisibleCharacters.value - visibleDraftLength.value,
));
const completionEligible = computed(() => (
  typeof session.value?.can_finalize === "boolean"
    ? session.value.can_finalize
    : validAnswerCount.value >= minimumValidAnswers.value
));
const progressLabel = computed(() => (
  completionEligible.value
    ? `有效回答 ${validAnswerCount.value}/${minimumValidAnswers.value} · 已达到完成条件`
    : `有效回答 ${validAnswerCount.value}/${minimumValidAnswers.value}`
));
const answerLengthHint = computed(() => {
  if (isClarificationText(draft.value)) return "澄清请求可直接提交，不计入有效回答。";
  if (isShortAnswerExempt.value) return "这类重要输入可直接提交。";
  if (isFirstValidAnswer.value) {
    return visibleDraftLength.value > 0
      ? "首次回答简短也可以，接下来会根据你的话继续聊。"
      : "首次回答简短也可以。";
  }
  if (remainingAnswerCharacters.value > 0) {
    return `可以再补充你这样想的原因、依据或一个具体例子（还差 ${remainingAnswerCharacters.value} 个有效字符）。`;
  }
  return "已达到本次回答的最低长度。";
});
const canSubmit = computed(() => (
  draft.value.trim().length > 0
  && meetsAnswerLength.value
  && isInterviewing.value
  && !sending.value
  && !loading.value
));
const canFinish = computed(() => (
  completionEligible.value
  && isInterviewing.value
  && !sending.value
  && !finalizing.value
  && !loading.value
));
const pendingKey = computed(() => `v6:pending-turn:${uuid.value}`);
const PENDING_TURN_TTL_MS = 24 * 60 * 60 * 1000;

function isTurnRequest(value: unknown): value is TurnRequest {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<TurnRequest>;
  return (
    typeof candidate.content === "string"
    && candidate.content.trim().length > 0
    && typeof candidate.client_turn_id === "string"
    && ["text", "voice", "voice_edited"].includes(String(candidate.input_mode))
    && (candidate.interaction_kind === undefined || ["answer", "clarification"].includes(String(candidate.interaction_kind)))
    && typeof candidate.answer_duration_ms === "number"
  );
}

function readPendingPayload(): TurnRequest | null {
  const raw = localStorage.getItem(pendingKey.value);
  if (!raw) return null;
  try {
    const envelope = JSON.parse(raw) as { saved_at?: unknown; payload?: unknown };
    if (
      typeof envelope.saved_at !== "number"
      || Date.now() - envelope.saved_at > PENDING_TURN_TTL_MS
      || envelope.saved_at > Date.now() + 60_000
      || !isTurnRequest(envelope.payload)
    ) {
      localStorage.removeItem(pendingKey.value);
      return null;
    }
    return envelope.payload;
  } catch {
    localStorage.removeItem(pendingKey.value);
    return null;
  }
}

const hasPending = ref(readPendingPayload() !== null);

function updateTurns(nextTurns: DialogueTurn[]) {
  const seen = new Set<string>();
  turns.value = [...nextTurns]
    .filter((turn) => {
      const key = String(turn.id ?? `${turn.turn_index}:${turn.role}:${turn.content}`);
      if (seen.has(key)) return false;
      seen.add(key);
      return turn.role !== "system";
    })
    .sort((a, b) => a.turn_index - b.turn_index);
}

function onDraftInput() {
  inputMode.value = voiceWasUsed.value ? "voice_edited" : "text";
  if (error.value.startsWith("这次回答还可以再补充")) error.value = "";
}

function onAnswerKeydown(event: KeyboardEvent) {
  if (
    event.key !== "Enter"
    || event.shiftKey
    || event.isComposing
    || event.repeat
  ) return;
  event.preventDefault();
  if (canSubmit.value) {
    void submitAnswer();
  } else if (draft.value.trim() && !meetsAnswerLength.value) {
    error.value = `这次回答还可以再补充 ${remainingAnswerCharacters.value} 个有效字符。也可说说你的原因、依据或一个具体例子。`;
  }
}

function toggleVoice() {
  if (voice.listening.value) voice.stop();
  else voice.start(draft.value);
}

function toggleTts() {
  ttsEnabled.value = !ttsEnabled.value;
  localStorage.setItem("v6:tts-enabled", String(ttsEnabled.value));
  if (!ttsEnabled.value) playback.stop();
}

function eventDelta(event: TurnStreamEvent): string {
  if (event.delta) return event.delta;
  if (typeof event.data === "string") return event.data;
  if (event.data && typeof event.data === "object") {
    const value = (event.data as Record<string, unknown>).delta;
    return typeof value === "string" ? value : "";
  }
  return "";
}

async function handleEvent(event: TurnStreamEvent) {
  if (event.event === "agent_delta") streamedText.value += eventDelta(event);
  if (event.event === "session_finalizing") {
    const snapshot = finalizingSession(event);
    if (snapshot) {
      session.value = snapshot;
      updateTurns(snapshot.turns ?? turns.value);
    } else if (session.value) {
      // The wire event intentionally carries only the close marker; keep the
      // existing transcript while reflecting its frozen state immediately.
      session.value = { ...session.value, phase: "finalizing" };
    }
    notice.value = "这段对话已经自然收束，正在整理报告…";
    return;
  }
  if (event.event === "error") {
    const dataMessage = event.data && typeof event.data === "object" ? (event.data as Record<string, unknown>).message : null;
    throw new Error(event.message || (typeof dataMessage === "string" ? dataMessage : "访谈处理出现异常"));
  }
  if (event.event !== "agent_completed") return;
  const completed = completedData(event);
  if (!completed) throw new Error("服务端未返回已保存的访谈内容");
  streamedText.value = "";
  if (completed.session) {
    session.value = completed.session;
    updateTurns(completed.session.turns ?? [...turns.value, completed.turn]);
  } else {
    updateTurns([...turns.value, completed.turn]);
  }
  localStorage.removeItem(pendingKey.value);
  hasPending.value = false;
  answerStartedAt.value = Date.now();
  if (session.value?.phase === "safety_stopped") {
    notice.value = "本次对话已暂停，不会继续提问或自动生成报告。";
  } else if (completed.session_action === "finish") {
    notice.value = "澄澄觉得这段对话已经自然收束，正在整理报告…";
  }
  if (ttsEnabled.value) void playback.speak(completed.turn.content, completed.speech_url);
}

async function synchronizeSession() {
  const snapshot = await getSession(uuid.value);
  session.value = snapshot;
  updateTurns(snapshot.turns ?? turns.value);
  return snapshot;
}

async function generateReport(automatic = false) {
  if (finalizing.value || leaving.value || !session.value) return;
  finalizing.value = true;
  error.value = "";
  notice.value = automatic ? "正在使用已冻结的逐字稿生成报告…" : "正在结束访谈并生成报告…";
  try {
    const result = await finalizeSession(uuid.value);
    session.value = result.session;
    if (result.session.phase === "completed" || result.report) {
      localStorage.removeItem("v6:last-session");
      await router.replace(`/assessment/report/${uuid.value}`);
      return;
    }
    notice.value = "访谈已冻结，报告仍在整理中。你可以稍后安全重试。";
  } catch (cause) {
    const body = cause instanceof ApiError && cause.body && typeof cause.body === "object"
      ? cause.body as Record<string, unknown>
      : null;
    const detail = body?.detail && typeof body.detail === "object"
      ? body.detail as Record<string, unknown>
      : null;
    const code = body?.code ?? detail?.code;
    const remainingFromServer = body?.remaining_answers ?? detail?.remaining_answers;
    const remaining = typeof remainingFromServer === "number"
      ? Math.max(0, Math.floor(remainingFromServer))
      : remainingRequiredAnswers.value;
    error.value = code === "minimum_valid_answers_not_reached"
      ? remaining > 0
        ? `这次访谈还需要完成 ${remaining} 次有效回答，再生成完整报告。`
        : "服务端尚未确认完成条件，请刷新后重试。已保存的回答不会丢失。"
      : cause instanceof ApiError
        ? cause.message
        : "报告生成失败。已保存的访谈不会丢失，可以安全重试。";
    try {
      const snapshot = await synchronizeSession();
      if (snapshot.phase === "completed" || snapshot.report_available) {
        localStorage.removeItem("v6:last-session");
        await router.replace(`/assessment/report/${uuid.value}`);
        return;
      }
      if (snapshot.phase === "finalizing") {
        notice.value = snapshot.finalization_state === "failed"
          ? "报告生成失败，已保存的访谈不会丢失，可以重试。"
          : "报告仍在生成中；若稍后仍未完成，可以安全重试。";
      } else {
        notice.value = "会话状态已同步，请根据当前状态继续操作。";
      }
    } catch {
      notice.value = "暂时无法同步报告状态；已保存的访谈不会丢失，请稍后重试。";
    }
  } finally {
    finalizing.value = false;
  }
}

async function finishAndGenerate() {
  if (!canFinish.value) return;
  if (!window.confirm("现在结束访谈并生成报告吗？报告只会使用已经保存的内容。")) return;
  draft.value = "";
  voice.stop();
  playback.stop();
  await generateReport();
}

async function submitContent(
  content: string,
  interactionKind: InteractionKind,
  clearDraft: boolean,
) {
  if (!content.trim() || sending.value) return;
  if (voice.listening.value) voice.stop();
  playback.stop();
  const payload: TurnRequest = {
    content: content.trim(),
    client_turn_id: crypto.randomUUID(),
    input_mode: interactionKind === "clarification" ? "text" : inputMode.value,
    interaction_kind: interactionKind,
    answer_duration_ms: Math.max(0, Date.now() - answerStartedAt.value),
  };
  localStorage.setItem(pendingKey.value, JSON.stringify({ saved_at: Date.now(), payload }));
  hasPending.value = true;
  if (clearDraft) {
    draft.value = "";
    inputMode.value = "text";
    voiceWasUsed.value = false;
  }
  await sendPayload(payload);
}

async function sendPayload(payload: TurnRequest, restoring = false) {
  sending.value = true;
  error.value = "";
  notice.value = restoring ? "正在恢复上次中断的提交…" : "";
  streamedText.value = "";
  const alreadySaved = turns.value.some((turn) => turn.client_turn_id === payload.client_turn_id);
  if (!alreadySaved) {
    const maxIndex = Math.max(-1, ...turns.value.map((turn) => turn.turn_index));
    updateTurns([
      ...turns.value,
      {
        turn_index: maxIndex + 1,
        role: "user",
        content: payload.content,
        client_turn_id: payload.client_turn_id,
        input_mode: payload.input_mode,
        answer_duration_ms: payload.answer_duration_ms,
        phase: session.value?.phase,
      },
    ]);
  }
  try {
    const controller = new AbortController();
    activeController = controller;
    await submitTurnStream(uuid.value, payload, handleEvent, controller.signal);
    if (leaving.value) return;
    const snapshot = await synchronizeSession();
    localStorage.removeItem(pendingKey.value);
    hasPending.value = false;
    notice.value = snapshot.phase === "safety_stopped"
      ? "本次对话已暂停，不会继续提问或自动生成报告。"
      : "";
    if (snapshot.phase === "completed") {
      localStorage.removeItem("v6:last-session");
      await router.replace(`/assessment/report/${uuid.value}`);
    } else if (snapshot.phase === "finalizing") {
      await generateReport(true);
    }
  } catch (cause) {
    if (!leaving.value) {
      error.value = cause instanceof ApiError || cause instanceof Error ? cause.message : "提交中断；刷新页面会使用相同编号恢复，不会重复计入。";
    }
  } finally {
    activeController = null;
    sending.value = false;
  }
}

async function submitAnswer() {
  const content = draft.value.trim();
  if (!content || sending.value) return;
  if (!meetsAnswerLength.value) {
    error.value = `这次回答还可以再补充 ${remainingAnswerCharacters.value} 个有效字符。也可说说你的原因、依据或一个具体例子。`;
    return;
  }
  const interactionKind: InteractionKind = isClarificationText(content) ? "clarification" : "answer";
  await submitContent(content, interactionKind, true);
}

async function requestClarification() {
  if (!isInterviewing.value || sending.value || finalizing.value || loading.value) return;
  await submitContent(CLARIFICATION_REQUEST, "clarification", false);
}

async function recoverPending() {
  const payload = readPendingPayload();
  hasPending.value = payload !== null;
  if (!payload || !isInterviewing.value) return;
  await sendPayload(payload, true);
}

function clearLocalRecovery() {
  localStorage.removeItem(pendingKey.value);
  localStorage.removeItem("v6:last-session");
  hasPending.value = false;
}

async function leaveEarly() {
  if (!window.confirm("退出不会生成报告。已经保存的记录会保留在本地复核范围内，确定退出吗？")) return;
  leaving.value = true;
  activeController?.abort();
  activeController = null;
  playback.stop();
  voice.stop();
  clearLocalRecovery();
  try {
    session.value = await exitSession(uuid.value);
  } catch {
    // Local exit remains available if the local service is temporarily offline.
  }
  await router.push("/assessment");
}

async function load() {
  loading.value = true;
  try {
    const snapshot = await synchronizeSession();
    if (snapshot.phase === "exited" || snapshot.phase === "safety_stopped") {
      clearLocalRecovery();
      return;
    }
    localStorage.setItem("v6:last-session", uuid.value);
    if (snapshot.phase === "completed") {
      localStorage.removeItem("v6:last-session");
      await router.replace(`/assessment/report/${uuid.value}`);
      return;
    }
    if (snapshot.phase === "finalizing") {
      localStorage.removeItem(pendingKey.value);
      hasPending.value = false;
      return;
    }
    await recoverPending();
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "无法恢复访谈，请确认本地服务已启动。";
  } finally {
    loading.value = false;
  }
}

watch(
  () => [turns.value.length, streamedText.value],
  async () => {
    await nextTick();
    transcriptEnd.value?.scrollIntoView({ behavior: "smooth", block: "end" });
  },
);

watch(playback.error, (message) => {
  if (message) notice.value = message;
});

onMounted(load);
onBeforeRouteLeave(async (to) => {
  if (
    leaving.value
    || session.value?.phase === "completed"
    || session.value?.phase === "exited"
    || session.value?.phase === "safety_stopped"
    || to.path.startsWith("/assessment/report/")
  ) return true;
  if (!window.confirm("离开将退出当前访谈且不生成报告，确定继续吗？")) return false;
  leaving.value = true;
  activeController?.abort();
  playback.stop();
  voice.stop();
  clearLocalRecovery();
  await exitSession(uuid.value).catch(() => null);
  return true;
});
onBeforeUnmount(() => {
  activeController?.abort();
});
</script>

<template>
  <main class="interview-page">
    <header class="interview-header">
      <button type="button" class="brand compact brand-button" @click="leaveEarly"><span>思衡</span><small>V6</small></button>
      <span v-if="session" class="round-count" aria-live="polite">{{ progressLabel }}</span>
      <button type="button" class="quiet-button" @click="leaveEarly">退出访谈</button>
    </header>

    <section v-if="loading" class="center-state"><span class="loading-ring" />正在恢复访谈…</section>
    <section v-else-if="session" class="interview-layout">
      <aside class="interviewer-panel">
        <InterviewerAvatar :state="interviewerState" />
        <div class="interviewer-copy">
          <strong>访谈官 · 澄澄</strong>
          <span v-if="voice.listening.value">我在听，转写后你可以继续修改</span>
          <span v-else-if="interviewerState === 'thinking'">正在回应你刚才说的话</span>
          <span v-else-if="interviewerState === 'speaking'">正在播报，开始录音会立即停止</span>
        </div>
        <button type="button" class="tts-toggle" :aria-pressed="ttsEnabled" @click="toggleTts">
          <span :class="{ active: ttsEnabled }" />语音播报 {{ ttsEnabled ? "已开" : "已关" }}
        </button>
        <small v-if="playback.fallbackUsed.value" class="fallback-note">服务端语音不可用，已切换浏览器播报</small>
      </aside>

      <section class="conversation-panel">
        <div class="transcript" aria-live="polite">
          <article v-for="turn in turns" :key="turn.id ?? `${turn.turn_index}-${turn.role}`" class="message" :class="turn.role">
            <span class="message-author">{{ turn.role === "assistant" ? "澄澄" : "你" }}</span>
            <p>{{ turn.content }}</p>
            <small v-if="turn.role === 'user' && turn.input_mode">{{ turn.input_mode === "text" ? "文字输入" : turn.input_mode === "voice" ? "语音转写" : "语音转写后编辑" }}</small>
          </article>
          <article v-if="streamedText" class="message assistant streaming">
            <span class="message-author">澄澄</span><p>{{ streamedText }}<i class="typing-caret" /></p>
          </article>
          <div ref="transcriptEnd" />
        </div>

        <div v-if="notice" class="notice-banner" role="status">{{ notice }}</div>
        <div v-if="error" class="error-banner" role="alert">
          {{ error }}
          <button v-if="hasPending" type="button" @click="recoverPending">用原提交编号重试</button>
        </div>

        <div v-if="session.phase === 'finalizing'" class="finalizing-card">
          <div><strong>正在整理这次访谈</strong><span>报告只会使用已保存的原话。生成失败时可以安全重试，不会重新提问。</span></div>
          <button type="button" class="primary-button small" :disabled="finalizing" @click="() => generateReport()">
            {{ finalizing ? "正在生成…" : "重试生成报告" }}
          </button>
        </div>

        <div v-else-if="session.phase === 'exited'" class="finalizing-card exited-card">
          本次访谈已退出，不会继续接收回答或生成报告。
          <RouterLink class="quiet-link" to="/assessment">开始新访谈</RouterLink>
        </div>

        <div v-else-if="session.phase === 'safety_stopped'" class="finalizing-card safety-card">
          <div><strong>本次对话已停止</strong><span>为避免继续处理可能需要即时线下支持的内容，系统不会继续提问或自动生成报告。</span></div>
          <RouterLink class="quiet-link" to="/assessment">开始新访谈</RouterLink>
        </div>

        <form v-else class="answer-composer" @submit.prevent="submitAnswer">
          <label for="answer-input">你的回答</label>
          <textarea
            id="answer-input"
            v-model="draft"
            rows="4"
            maxlength="4000"
            placeholder="按你此刻真实的想法说就好；没听明白可点“换个问法”，不确定时也可以说说不确定在哪里…"
            :disabled="sending || finalizing"
            @input="onDraftInput"
            @keydown="onAnswerKeydown"
          />
          <div class="composer-actions">
            <div>
              <button
                type="button"
                class="mic-button"
                :class="{ recording: voice.listening.value }"
                :disabled="!voice.supported.value || sending || finalizing"
                :aria-pressed="voice.listening.value"
                @click="toggleVoice"
              >
                <span class="mic-icon" aria-hidden="true" />{{ voice.listening.value ? "停止转写" : "语音输入" }}
              </button>
              <small v-if="voice.error.value">{{ voice.error.value }}</small>
              <small v-else-if="voiceWasUsed">转写已放入文本框，请确认或修改后手动提交</small>
            </div>
            <span class="char-count" :class="{ insufficient: draft.trim() && !meetsAnswerLength }" aria-live="polite">
              {{ visibleDraftLength }}/{{ requiredVisibleCharacters }}
              <small>{{ answerLengthHint }}</small>
            </span>
            <button class="secondary-button compact-action" type="button" :disabled="!canFinish" @click="finishAndGenerate">
              {{ completionEligible ? "结束并生成报告" : `完成 ${minimumValidAnswers} 次后可生成报告` }}
            </button>
            <button class="send-button" type="submit" :disabled="!canSubmit">
              {{ sending ? "正在回应…" : "提交回答" }}<span aria-hidden="true">↑</span>
            </button>
          </div>
          <div class="conversation-shortcuts" aria-label="访谈辅助操作">
            <span>没听明白或不想继续时，可以直接选择：</span>
            <button type="button" :disabled="sending || finalizing" @click="requestClarification">没理解，请换个问法</button>
            <button type="button" :disabled="sending || finalizing" @click="leaveEarly">退出访谈（不生成完整报告）</button>
          </div>
        </form>
      </section>
    </section>
    <section v-else class="center-state error-state"><p>{{ error || "无法打开访谈。" }}</p><RouterLink class="secondary-button" to="/assessment">返回开始页</RouterLink></section>
  </main>
</template>
