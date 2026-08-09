<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { onBeforeRouteLeave, useRoute, useRouter } from "vue-router";
import InterviewerAvatar from "@/components/InterviewerAvatar.vue";
import { ApiError } from "@/api/http";
import {
  checkReportReadiness,
  completedData,
  exitSession,
  finalizingSession,
  finalizeSession,
  getSession,
  submitTurnStream,
} from "@/api/session";
import { useSpeechPlayback, useVoiceInput } from "@/composables/useVoice";
import type {
  DialogueTurn,
  InputMode,
  InterviewerState,
  FinalizeSessionRequest,
  ReportReadinessResponse,
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
const checkingReadiness = ref(false);
const evidenceReadiness = ref<ReportReadinessResponse | null>(null);
const error = ref("");
const notice = ref("");
const inputMode = ref<InputMode>("text");
const voiceWasUsed = ref(false);
const answerStartedAt = ref(Date.now());
const transcriptEnd = ref<HTMLElement | null>(null);
const answerInput = ref<HTMLTextAreaElement | null>(null);
const voiceInputEnabled = import.meta.env.VITE_VOICE_INPUT_ENABLED === "true";
const ttsEnabled = ref(localStorage.getItem("v6:tts-enabled") !== "false");
const leaving = ref(false);
const playback = useSpeechPlayback();
let activeController: AbortController | null = null;
let savedWaitTimer: number | null = null;
let extendedWaitTimer: number | null = null;
let readinessPollTimer: number | null = null;
let readinessPollGeneration = 0;

function clearInterviewWaitTimers() {
  if (savedWaitTimer !== null) window.clearTimeout(savedWaitTimer);
  if (extendedWaitTimer !== null) window.clearTimeout(extendedWaitTimer);
  savedWaitTimer = null;
  extendedWaitTimer = null;
}

function clearReadinessPolling() {
  readinessPollGeneration += 1;
  if (readinessPollTimer !== null) window.clearTimeout(readinessPollTimer);
  readinessPollTimer = null;
}

function startInterviewWaitTimers() {
  clearInterviewWaitTimers();
  savedWaitTimer = window.setTimeout(() => {
    if (sending.value && !leaving.value) {
      notice.value = "正在整理这条回答；本次提交已在本地保留。";
    }
  }, 8_000);
  extendedWaitTimer = window.setTimeout(() => {
    if (sending.value && !leaving.value) {
      notice.value = "仍在处理中；如果中断，可以使用原提交编号安全重试。";
    }
  }, 20_000);
}

const MIN_ANSWER_VISIBLE_CHARACTERS = 20;
const MIN_ANSWER_MESSAGE = `从第二个回答起，每次回答至少需要 ${MIN_ANSWER_VISIBLE_CHARACTERS} 个字。`;
const EXPLICIT_UNCERTAINTY_PATTERN = /^(?:我)?(?:现在|暂时|目前|还|也|确实|真的){0,2}(?:不知道(?:(?:该|要)?怎么(?:说|回答))?|不清楚|不太清楚|不确定|不太确定|没想好|没有想好|没想法|没有想法|没什么想法|没有什么想法|想不到|说不上来|不会回答)(?:了|呢|啊|吧)?$/u;

function visibleCharacterCount(value: string): number {
  return Array.from(value).filter((character) => !/\s/u.test(character)).length;
}

function normalizedShortAnswerText(value: string): string {
  return Array.from(value.normalize("NFKC").toLocaleLowerCase())
    .filter((character) => !/[\s\p{P}]/u.test(character))
    .join("");
}

function isExplicitUncertaintyAnswer(value: string): boolean {
  return EXPLICIT_UNCERTAINTY_PATTERN.test(normalizedShortAnswerText(value));
}

const interviewerState = computed<InterviewerState>(() => {
  if (voice.listening.value) return "listening";
  if (playback.speaking.value) return "speaking";
  if (sending.value || finalizing.value || checkingReadiness.value) return "thinking";
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
const visibleDraftLength = computed(() => visibleCharacterCount(draft.value));
const roundCount = computed(() => turns.value.filter((turn) => turn.role === "user").length);
const savedAnswerCount = computed(() => session.value?.user_answer_count ?? roundCount.value);
const technicalTurnCapReached = computed(() => (
  session.value?.technical_turn_cap_reached === true
  || (
    typeof session.value?.technical_turn_cap === "number"
    && savedAnswerCount.value >= session.value.technical_turn_cap
  )
));
const isFirstAnswer = computed(() => savedAnswerCount.value === 0);
const isUncertaintyAnswer = computed(() => isExplicitUncertaintyAnswer(draft.value));
const meetsAnswerRequirement = computed(() => (
  isFirstAnswer.value
  || visibleDraftLength.value >= MIN_ANSWER_VISIBLE_CHARACTERS
  || isUncertaintyAnswer.value
));
const remainingAnswerCharacters = computed(() => (
  isFirstAnswer.value || isUncertaintyAnswer.value
    ? 0
    : Math.max(0, MIN_ANSWER_VISIBLE_CHARACTERS - visibleDraftLength.value)
));
const answerRequirementHint = computed(() => {
  if (isFirstAnswer.value) return "首次可简短；之后每次至少 20 字";
  if (isUncertaintyAnswer.value) return "可以直接提交";
  if (remainingAnswerCharacters.value > 0) {
    return `至少 ${MIN_ANSWER_VISIBLE_CHARACTERS} 字，还差 ${remainingAnswerCharacters.value} 字`;
  }
  return "可以提交";
});
const answerPlaceholder = "按你此刻真实的想法说就好…";
const canSubmit = computed(() => (
  draft.value.trim().length > 0
  && meetsAnswerRequirement.value
  && isInterviewing.value
  && !technicalTurnCapReached.value
  && !sending.value
  && !checkingReadiness.value
  && !loading.value
));
const canFinish = computed(() => (
  isInterviewing.value
  && !sending.value
  && !finalizing.value
  && !checkingReadiness.value
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
  if (error.value === MIN_ANSWER_MESSAGE) error.value = "";
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
  } else if (draft.value.trim() && !meetsAnswerRequirement.value) {
    error.value = MIN_ANSWER_MESSAGE;
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
  if (event.event === "session_closure_suggested") {
    clearInterviewWaitTimers();
    // Historical natural-close events are deliberately not rendered in V6.2.
    // Only the independent evidence snapshot may expose a finish entry.
    return;
  }
  if (event.event === "session_finalizing") {
    clearInterviewWaitTimers();
    const snapshot = finalizingSession(event);
    if (snapshot) {
      session.value = snapshot;
      updateTurns(snapshot.turns ?? turns.value);
    } else if (session.value) {
      // The wire event intentionally carries only the close marker; keep the
      // existing transcript while reflecting its frozen state immediately.
      session.value = { ...session.value, phase: "finalizing" };
    }
    notice.value = "对话已冻结，正在整理报告…";
    return;
  }
  if (event.event === "error") {
    clearInterviewWaitTimers();
    const dataMessage = event.data && typeof event.data === "object" ? (event.data as Record<string, unknown>).message : null;
    throw new Error(event.message || (typeof dataMessage === "string" ? dataMessage : "访谈处理出现异常"));
  }
  if (event.event !== "agent_completed") return;
  clearInterviewWaitTimers();
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
    notice.value = "对话已冻结，正在整理报告…";
  }
  if (ttsEnabled.value) void playback.speak(completed.turn.content, completed.speech_url);
}

async function synchronizeSession() {
  const snapshot = await getSession(uuid.value);
  session.value = snapshot;
  updateTurns(snapshot.turns ?? turns.value);
  return snapshot;
}

async function refreshEvidenceReadiness(generation = readinessPollGeneration) {
  if (!isInterviewing.value || sending.value || leaving.value) return;
  try {
    const result = await checkReportReadiness(uuid.value);
    if (generation !== readinessPollGeneration || !isInterviewing.value) return;
    evidenceReadiness.value = result;
    if (result.status === "failed") {
      notice.value = "当前证据结果暂未整理完成，可以继续回答或稍后重试。";
    }
  } catch {
    if (generation === readinessPollGeneration && isInterviewing.value) {
      evidenceReadiness.value = null;
    }
  }
}

function startEvidenceReadinessPolling() {
  clearReadinessPolling();
  evidenceReadiness.value = null;
  const generation = readinessPollGeneration;
  const startedAt = Date.now();
  const poll = async () => {
    await refreshEvidenceReadiness(generation);
    if (
      generation !== readinessPollGeneration
      || !isInterviewing.value
      || evidenceReadiness.value?.status !== "checking"
      || Date.now() - startedAt >= 20_000
    ) return;
    readinessPollTimer = window.setTimeout(() => void poll(), 1_500);
  };
  void poll();
}

async function waitForCurrentEvidenceSnapshot(): Promise<ReportReadinessResponse | null> {
  const deadline = Date.now() + 16_000;
  let retryFailed = evidenceReadiness.value?.status === "failed";
  while (Date.now() < deadline) {
    try {
      const current = await checkReportReadiness(uuid.value, retryFailed);
      retryFailed = false;
      evidenceReadiness.value = current;
      if (current.status !== "checking") return current;
    } catch {
      return null;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 750));
  }
  return evidenceReadiness.value?.status === "checking"
    ? evidenceReadiness.value
    : null;
}

async function generateReport(
  automatic = false,
  request: FinalizeSessionRequest = {},
) {
  if (finalizing.value || leaving.value || !session.value) return;
  clearReadinessPolling();
  finalizing.value = true;
  error.value = "";
  notice.value = automatic ? "正在使用已冻结的逐字稿生成报告…" : "正在结束访谈并生成报告…";
  try {
    const result = await finalizeSession(uuid.value, request);
    session.value = result.session;
    if (result.session.phase === "completed" || result.report) {
      localStorage.removeItem("v6:last-session");
      await router.replace(`/assessment/report/${uuid.value}`);
      return;
    }
    notice.value = "访谈已冻结，报告仍在整理中。你可以稍后安全重试。";
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "报告生成失败。已保存的访谈不会丢失，可以安全重试。";
    try {
      const snapshot = await synchronizeSession();
      if (snapshot.phase === "completed" || snapshot.report_available) {
        localStorage.removeItem("v6:last-session");
        await router.replace(`/assessment/report/${uuid.value}`);
        return;
      }
      notice.value = snapshot.phase === "finalizing"
        ? snapshot.finalization_state === "failed"
          ? "报告生成失败，已保存的访谈不会丢失，可以重试。"
          : "报告仍在生成中；稍后可以安全重试。"
        : "会话状态已同步，请根据当前状态继续操作。";
    } catch {
      notice.value = "暂时无法同步报告状态；已保存的访谈不会丢失，请稍后重试。";
    }
  } finally {
    finalizing.value = false;
  }
}

async function confirmReportGeneration(): Promise<FinalizeSessionRequest | null> {
  checkingReadiness.value = true;
  error.value = "";
  notice.value = "正在整理最新一轮的证据…";
  try {
    const readiness = await waitForCurrentEvidenceSnapshot();
    if (!readiness) {
      notice.value = "当前证据结果暂未整理完成，请稍后重试。";
      return null;
    }
    if (readiness.status === "checking") {
      notice.value = "最新证据仍在整理中，完成后即可快速生成报告。";
      return null;
    }
    if (readiness.status === "failed") {
      notice.value = "当前证据结果暂未整理完成，请重试；已保存的回答不会丢失。";
      return null;
    }
    const baseRequest: FinalizeSessionRequest = {
      evidence_check_id: readiness.check_id,
      expected_transcript_fingerprint: readiness.transcript_fingerprint,
      allow_incomplete: false,
    };
    if (readiness.status === "ready" && readiness.ready === true) {
      const shouldGenerate = window.confirm(
        "按当前终评证据规则，现有回答已达到报告准备条件。现在结束访谈并生成报告吗？",
      );
      notice.value = shouldGenerate ? "" : "现有回答已达到报告准备条件，你可以继续说，也可以随时生成报告。";
      return shouldGenerate ? baseRequest : null;
    }
    if (readiness.status === "insufficient" && readiness.ready === false) {
      const message = technicalTurnCapReached.value
        ? "按当前终评证据规则，现有回答可能还不足以支持完整报告。本次访谈已达到技术保护上限，你仍可根据已有回答生成报告，证据有限的部分会如实说明。是否仍然生成？"
        : "现有回答尚不足以支持完整报告。你可以继续访谈，也可以现在生成报告，证据有限的部分会如实说明。是否仍然生成？";
      const shouldGenerate = window.confirm(message);
      notice.value = shouldGenerate
        ? ""
        : technicalTurnCapReached.value
          ? "本次访谈已达到技术保护上限；你仍可根据已有回答生成报告。"
          : "建议继续访谈，补充更多可核对的具体经历、理由和判断依据。";
      return shouldGenerate
        ? { ...baseRequest, allow_incomplete: true }
        : null;
    }
  } catch {
    notice.value = "当前证据结果暂未整理完成，请稍后重试。";
    return null;
  } finally {
    checkingReadiness.value = false;
  }
  return null;
}

async function finishAndGenerate() {
  if (!canFinish.value) return;
  const request = await confirmReportGeneration();
  if (!request) return;
  draft.value = "";
  voice.stop();
  playback.stop();
  await generateReport(false, request);
}

function continueAfterEvidenceReady() {
  evidenceReadiness.value = null;
  notice.value = "你可以继续补充；已提交的回答和证据结果都会保留。";
  answerStartedAt.value = Date.now();
  void nextTick(() => answerInput.value?.focus());
}

async function sendPayload(payload: TurnRequest, restoring = false) {
  sending.value = true;
  error.value = "";
  notice.value = restoring ? "正在恢复上次中断的提交…" : "";
  startInterviewWaitTimers();
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
      notice.value = "";
      error.value = cause instanceof ApiError || cause instanceof Error ? cause.message : "提交中断；刷新页面会使用相同编号恢复，不会重复计入。";
    }
  } finally {
    clearInterviewWaitTimers();
    activeController = null;
    sending.value = false;
    if (isInterviewing.value && !leaving.value) {
      startEvidenceReadinessPolling();
    }
  }
}

async function submitAnswer() {
  const content = draft.value.trim();
  if (!content || sending.value) return;
  if (!meetsAnswerRequirement.value) {
    error.value = MIN_ANSWER_MESSAGE;
    return;
  }
  if (voice.listening.value) voice.stop();
  clearReadinessPolling();
  evidenceReadiness.value = null;
  playback.stop();
  const payload: TurnRequest = {
    content,
    client_turn_id: crypto.randomUUID(),
    input_mode: inputMode.value,
    answer_duration_ms: Math.max(0, Date.now() - answerStartedAt.value),
  };
  localStorage.setItem(pendingKey.value, JSON.stringify({ saved_at: Date.now(), payload }));
  hasPending.value = true;
  draft.value = "";
  inputMode.value = "text";
  voiceWasUsed.value = false;
  await sendPayload(payload);
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
  if (!window.confirm("退出不会生成报告。已经提交的内容仍会按开始页说明保留，确定退出吗？")) return;
  leaving.value = true;
  clearInterviewWaitTimers();
  clearReadinessPolling();
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
    if (snapshot.phase === "interviewing" && !hasPending.value && savedAnswerCount.value > 0) {
      startEvidenceReadinessPolling();
    }
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
  clearInterviewWaitTimers();
  clearReadinessPolling();
  activeController?.abort();
  playback.stop();
  voice.stop();
  clearLocalRecovery();
  await exitSession(uuid.value).catch(() => null);
  return true;
});
onBeforeUnmount(() => {
  clearInterviewWaitTimers();
  clearReadinessPolling();
  activeController?.abort();
});
</script>

<template>
  <main class="interview-page">
    <header class="interview-header">
      <button type="button" class="brand compact brand-button" @click="leaveEarly"><span>思衡</span><small>V6</small></button>
      <span v-if="session" class="round-count" aria-live="polite">已进行 {{ roundCount }} 轮问答</span>
      <button type="button" class="quiet-button" @click="leaveEarly">退出不生成报告</button>
    </header>

    <section v-if="loading" class="center-state"><span class="loading-ring" />正在恢复访谈…</section>
    <section v-else-if="session" class="interview-layout">
      <aside class="interviewer-panel">
        <InterviewerAvatar :state="interviewerState" />
        <div class="interviewer-copy">
          <strong>访谈官 · 澄澄</strong>
          <span v-if="voiceInputEnabled && voice.listening.value">我在听，转写后你可以继续修改</span>
          <span v-else-if="interviewerState === 'thinking'">正在回应你刚才说的话</span>
          <span v-else-if="interviewerState === 'speaking'">
            {{ voiceInputEnabled ? "正在播报，开始录音会立即停止" : "正在播报当前回复" }}
          </span>
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
            <small
              v-if="turn.role === 'user' && turn.input_mode && (voiceInputEnabled || turn.input_mode === 'text')"
            >{{ turn.input_mode === "text" ? "文字输入" : turn.input_mode === "voice" ? "语音转写" : "语音转写后编辑" }}</small>
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

        <section
          v-else-if="evidenceReadiness?.status === 'ready' && evidenceReadiness.ready === true"
          class="closure-suggestion-card evidence-ready-card"
          aria-labelledby="evidence-ready-title"
        >
          <div>
            <strong id="evidence-ready-title">现有回答已足够生成完整报告</strong>
          </div>
          <div class="closure-suggestion-actions">
            <button
              type="button"
              class="secondary-button"
              :disabled="sending || checkingReadiness || finalizing"
              @click="continueAfterEvidenceReady"
            >继续补充</button>
            <button
              type="button"
              class="primary-button"
              :disabled="!canFinish"
              @click="finishAndGenerate"
            >{{ checkingReadiness ? "正在确认…" : finalizing ? "正在生成…" : "结束并生成报告" }}</button>
          </div>
        </section>

        <div v-else-if="technicalTurnCapReached" class="finalizing-card technical-limit-card" role="status">
          <div>
            <strong>本次访谈已达到系统的技术保护上限</strong>
            <span>你此前的回答均已保存。为避免对话过长影响稳定性，本次不再接收新回答；这不代表系统在判定证据已充分。你可以现在结束访谈，并根据已有内容生成报告。</span>
          </div>
          <button type="button" class="primary-button small" :disabled="!canFinish" @click="finishAndGenerate">
            {{ checkingReadiness ? "正在检查…" : "结束并生成报告" }}
          </button>
        </div>

        <form v-else class="answer-composer" @submit.prevent="submitAnswer">
          <label for="answer-input">你的回答</label>
          <textarea
            id="answer-input"
            ref="answerInput"
            v-model="draft"
            rows="4"
            maxlength="4000"
            :placeholder="answerPlaceholder"
            aria-describedby="answer-requirement"
            :disabled="sending || finalizing || checkingReadiness"
            @input="onDraftInput"
            @keydown="onAnswerKeydown"
          />
          <div class="composer-actions">
            <div v-if="voiceInputEnabled">
              <button
                type="button"
                class="mic-button"
                :class="{ recording: voice.listening.value }"
                :disabled="!voice.supported.value || sending || finalizing || checkingReadiness"
                :aria-pressed="voice.listening.value"
                @click="toggleVoice"
              >
                <span class="mic-icon" aria-hidden="true" />{{ voice.listening.value ? "停止转写" : "语音输入" }}
              </button>
              <small v-if="voice.error.value">{{ voice.error.value }}</small>
              <small v-else-if="voiceWasUsed">转写已放入文本框，请确认或修改后手动提交</small>
            </div>
            <span
              id="answer-requirement"
              class="char-count"
              :class="{ insufficient: draft.trim() && remainingAnswerCharacters > 0 }"
              role="status"
              aria-live="polite"
            >
              {{ answerRequirementHint }}
            </span>
            <button class="secondary-button compact-action" type="button" :disabled="!canFinish" @click="finishAndGenerate">
              {{ checkingReadiness ? "正在检查…" : "结束并生成报告" }}
            </button>
            <button class="send-button" type="submit" :disabled="!canSubmit">
              {{ sending ? "正在回应…" : "提交回答" }}<span aria-hidden="true">↑</span>
            </button>
          </div>
        </form>
      </section>
    </section>
    <section v-else class="center-state error-state"><p>{{ error || "无法打开访谈。" }}</p><RouterLink class="secondary-button" to="/assessment">返回开始页</RouterLink></section>
  </main>
</template>
