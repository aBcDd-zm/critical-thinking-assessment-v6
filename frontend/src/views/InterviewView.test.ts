import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import InterviewView from "./InterviewView.vue";

const mocks = vi.hoisted(() => ({
  getSession: vi.fn(),
  checkReportReadiness: vi.fn(),
  acceptClosureSuggestion: vi.fn(),
  finalizeSession: vi.fn(),
  exitSession: vi.fn(),
  submitTurnStream: vi.fn(),
  replace: vi.fn(),
  push: vi.fn(),
}));

vi.mock("@/api/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/session")>();
  return {
    ...actual,
    getSession: mocks.getSession,
    checkReportReadiness: mocks.checkReportReadiness,
    acceptClosureSuggestion: mocks.acceptClosureSuggestion,
    finalizeSession: mocks.finalizeSession,
    exitSession: mocks.exitSession,
    submitTurnStream: mocks.submitTurnStream,
  };
});
vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { sessionUuid: "session-v6" } }),
  useRouter: () => ({ replace: mocks.replace, push: mocks.push }),
  onBeforeRouteLeave: vi.fn(),
}));

const openingTurn = { id: 1, turn_index: 0, role: "assistant" as const, phase: "interviewing", content: "你想从哪里开始聊？" };
const validAnswer = "我正在认真比较这个选择，也想把影响决定的现实条件想清楚。";

describe("InterviewView", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    localStorage.setItem("v6:tts-enabled", "false");
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 0,
      turns: [openingTurn],
    });
    mocks.checkReportReadiness.mockResolvedValue({
      status: "ready",
      ready: true,
      cached: false,
    });
    mocks.finalizeSession.mockResolvedValue({
      session: { uuid: "session-v6", phase: "completed", turns: [], report_available: true },
    });
    mocks.acceptClosureSuggestion.mockResolvedValue({
      session: { uuid: "session-v6", phase: "completed", turns: [], report_available: true, closure_suggestion: null },
    });
  });

  it("shows a single natural interviewer with a factual answer count but no stage or answer quota", async () => {
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("访谈官 · 澄澄");
    expect(wrapper.get(".round-count").text()).toBe("已进行 0 轮问答");
    expect(wrapper.text()).toContain("结束并生成报告");
    expect(wrapper.text()).not.toContain("语音输入");
    expect(wrapper.text()).not.toContain("停止转写");
    expect(wrapper.text()).not.toContain("转写已放入文本框");
    expect(wrapper.text()).toContain("语音播报");
    expect(wrapper.find(".natural-interview-note").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("最多 12");
    expect(wrapper.text()).not.toContain("问题界定");
    expect(wrapper.text()).not.toContain("覆盖率");
    expect(wrapper.text()).not.toContain("自然访谈 · 实验性");
    expect(wrapper.text()).not.toContain("从你愿意说的地方开始就好");
    expect(wrapper.find(".progress-block").exists()).toBe(false);

    await wrapper.get("button.compact-action").trigger("click");
    await flushPromises();

    expect(mocks.checkReportReadiness).toHaveBeenCalledWith("session-v6");
    expect(mocks.finalizeSession).toHaveBeenCalledWith("session-v6");
    expect(mocks.replace).toHaveBeenCalledWith("/assessment/report/session-v6");
  });

  it("recommends continuing when evidence is insufficient without exposing scoring details", async () => {
    mocks.checkReportReadiness.mockResolvedValueOnce({
      status: "insufficient",
      ready: false,
      cached: false,
    });
    vi.mocked(window.confirm).mockReturnValueOnce(false);

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("button.compact-action").trigger("click");
    await flushPromises();

    expect(mocks.checkReportReadiness).toHaveBeenCalledWith("session-v6");
    expect(mocks.finalizeSession).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("建议继续访谈");
    expect(wrapper.text()).not.toContain("问题界定");
    expect(wrapper.text()).not.toContain("证据评估");
    expect(wrapper.text()).not.toContain("引文");
  });

  it("allows a participant to generate a report despite insufficient evidence", async () => {
    mocks.checkReportReadiness.mockResolvedValueOnce({
      status: "insufficient",
      ready: false,
      cached: true,
    });

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("button.compact-action").trigger("click");
    await flushPromises();

    expect(mocks.finalizeSession).toHaveBeenCalledWith("session-v6");
    expect(mocks.replace).toHaveBeenCalledWith("/assessment/report/session-v6");
  });

  it("fails open when readiness cannot be checked and the participant confirms", async () => {
    mocks.checkReportReadiness.mockRejectedValueOnce(new Error("readiness unavailable"));

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("button.compact-action").trigger("click");
    await flushPromises();

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("仍然按现有回答生成报告"));
    expect(mocks.finalizeSession).toHaveBeenCalledWith("session-v6");
  });

  it("does not misdescribe an in-progress check as insufficient evidence", async () => {
    mocks.checkReportReadiness.mockResolvedValueOnce({
      status: "checking",
      ready: null,
      cached: true,
    });
    vi.mocked(window.confirm).mockReturnValueOnce(false);

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("button.compact-action").trigger("click");
    await flushPromises();

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("仍在检查中"));
    expect(mocks.finalizeSession).not.toHaveBeenCalled();
    expect(wrapper.text()).not.toContain("证据不足");
  });

  it("shows the editable voice-input controls only when the build flag is explicitly enabled", async () => {
    vi.stubEnv("VITE_VOICE_INPUT_ENABLED", "true");
    mocks.getSession.mockResolvedValueOnce({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 1,
      turns: [
        openingTurn,
        { id: 2, turn_index: 1, role: "user", content: validAnswer, input_mode: "voice" },
      ],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("语音输入");
    expect(wrapper.text()).toContain("语音转写");
    expect(wrapper.get("button.mic-button").attributes("aria-pressed")).toBe("false");
    expect(wrapper.text()).toContain("语音播报");
  });

  it("hides voice-origin labels from restored history when voice input is disabled", async () => {
    mocks.getSession.mockResolvedValueOnce({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 1,
      turns: [
        openingTurn,
        { id: 2, turn_index: 1, role: "user", content: validAnswer, input_mode: "voice_edited" },
      ],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.text()).not.toContain("语音转写");
    expect(wrapper.text()).not.toContain("语音转写后编辑");
    expect(wrapper.text()).not.toContain("语音输入");
    expect(wrapper.text()).toContain("语音播报");
  });

  it("restores a persisted close suggestion after refresh and lets the participant continue", async () => {
    const fingerprint = "c".repeat(64);
    mocks.getSession.mockResolvedValueOnce({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 1,
      closure_suggestion: {
        closure_turn_id: 3,
        transcript_fingerprint: fingerprint,
        finish_reason: "natural_closure",
      },
      turns: [
        openingTurn,
        { id: 2, turn_index: 1, role: "user", content: validAnswer },
        { id: 3, turn_index: 2, role: "assistant", content: "这里似乎是一个自然的停点。", session_action: "suggest_finish", finish_reason: "natural_closure" },
      ],
    });

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.get(".closure-suggestion-card").text()).toContain("继续交流");
    expect(wrapper.get(".closure-suggestion-card").text()).toContain("结束并生成报告");
    expect(wrapper.find("textarea").exists()).toBe(false);
    expect(mocks.checkReportReadiness).not.toHaveBeenCalled();
    expect(mocks.acceptClosureSuggestion).not.toHaveBeenCalled();
    expect(mocks.finalizeSession).not.toHaveBeenCalled();

    await wrapper.get(".closure-suggestion-card .secondary-button").trigger("click");
    await flushPromises();

    expect(wrapper.find(".closure-suggestion-card").exists()).toBe(false);
    expect(wrapper.find("textarea").exists()).toBe(true);
    expect(wrapper.text()).toContain("你可以继续补充");
    expect(mocks.checkReportReadiness).not.toHaveBeenCalled();
    expect(mocks.acceptClosureSuggestion).not.toHaveBeenCalled();
  });

  it("replays one close suggestion idempotently and only accepts it after participant confirmation", async () => {
    const fingerprint = "d".repeat(64);
    const suggestedSession = {
      uuid: "session-v6",
      phase: "interviewing" as const,
      user_answer_count: 1,
      closure_suggestion: {
        closure_turn_id: 3,
        transcript_fingerprint: fingerprint,
        finish_reason: "natural_closure" as const,
      },
      turns: [
        openingTurn,
        { id: 2, turn_index: 1, role: "user" as const, content: validAnswer, client_turn_id: "client-suggest-finish", input_mode: "text" as const, answer_duration_ms: 100 },
        { id: 3, turn_index: 2, role: "assistant" as const, content: "如果你愿意，我们可以在这里收束。", session_action: "suggest_finish" as const, finish_reason: "natural_closure" as const },
      ],
    };
    mocks.getSession
      .mockResolvedValueOnce({ uuid: "session-v6", phase: "interviewing", user_answer_count: 0, turns: [openingTurn] })
      .mockResolvedValueOnce(suggestedSession);
    mocks.submitTurnStream.mockImplementation(async (_uuid, _payload, onEvent) => {
      const suggestionEvent = {
        event: "session_closure_suggested",
        data: {
          session_uuid: "session-v6",
          closure_turn_id: 3,
          transcript_fingerprint: fingerprint,
          finish_reason: "natural_closure",
        },
      };
      await onEvent(suggestionEvent);
      await onEvent(suggestionEvent);
      await onEvent({
        event: "agent_completed",
        data: {
          turn: suggestedSession.turns[2],
          session_action: "suggest_finish",
          finish_reason: "natural_closure",
          session: suggestedSession,
        },
      });
    });

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("textarea").setValue(validAnswer);
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.findAll(".closure-suggestion-card")).toHaveLength(1);
    expect(mocks.checkReportReadiness).not.toHaveBeenCalled();
    expect(mocks.acceptClosureSuggestion).not.toHaveBeenCalled();
    expect(mocks.finalizeSession).not.toHaveBeenCalled();

    await wrapper.get(".closure-suggestion-card .primary-button").trigger("click");
    await flushPromises();

    expect(mocks.checkReportReadiness).toHaveBeenCalledTimes(1);
    expect(mocks.acceptClosureSuggestion).toHaveBeenCalledTimes(1);
    expect(mocks.acceptClosureSuggestion).toHaveBeenCalledWith(
      "session-v6",
      3,
      { expected_transcript_fingerprint: fingerprint },
    );
    expect(mocks.finalizeSession).not.toHaveBeenCalled();
    expect(mocks.replace).toHaveBeenCalledWith("/assessment/report/session-v6");
  });

  it("keeps the legacy automatic report path for older prompt versions that already froze the session", async () => {
    const finishedSession = {
      uuid: "session-v6",
      phase: "finalizing",
      user_answer_count: 1,
      turns: [
        openingTurn,
        { id: 2, turn_index: 1, role: "user" as const, content: validAnswer, client_turn_id: "client-natural-finish", input_mode: "text", answer_duration_ms: 100 },
        { id: 3, turn_index: 2, role: "assistant" as const, content: "谢谢你愿意说这些，我们先在这里收束。" },
      ],
    };
    mocks.getSession.mockResolvedValueOnce({ uuid: "session-v6", phase: "interviewing", turns: [openingTurn] });
    mocks.getSession.mockResolvedValueOnce(finishedSession);
    mocks.submitTurnStream.mockImplementation(async (_uuid, _payload, onEvent) => {
      await onEvent({
        event: "agent_completed",
        data: {
          turn: finishedSession.turns[2],
          session_action: "finish",
          finish_reason: "natural_closure",
        },
      });
    });

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("textarea").setValue(validAnswer);
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(mocks.finalizeSession).toHaveBeenCalledWith("session-v6");
    expect(mocks.replace).toHaveBeenCalledWith("/assessment/report/session-v6");
    expect(mocks.checkReportReadiness).not.toHaveBeenCalled();
  });

  it("rechecks the session when finalization times out before routing to the report", async () => {
    const completedSession = {
      uuid: "session-v6",
      phase: "completed" as const,
      turns: [openingTurn],
      report_available: true,
    };
    mocks.getSession
      .mockResolvedValueOnce({ uuid: "session-v6", phase: "interviewing", turns: [openingTurn] })
      .mockResolvedValueOnce(completedSession);
    mocks.finalizeSession.mockRejectedValue(new Error("请求超时"));

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("button.compact-action").trigger("click");
    await flushPromises();

    expect(mocks.getSession).toHaveBeenCalledTimes(2);
    expect(mocks.replace).toHaveBeenCalledWith("/assessment/report/session-v6");
  });

  it("does not describe a safety stop as a natural close or generate a report", async () => {
    const safetySession = {
      uuid: "session-v6",
      phase: "safety_stopped" as const,
      user_answer_count: 1,
      turns: [
        openingTurn,
        { id: 2, turn_index: 1, role: "user" as const, content: "我现在有立即危险，已经准备做伤害自己的事，而且身边暂时没有人。", client_turn_id: "client-safety-stop", input_mode: "text", answer_duration_ms: 100 },
        { id: 3, turn_index: 2, role: "assistant" as const, content: "我们先在这里停下，并优先获得现实支持。" },
      ],
    };
    mocks.getSession.mockResolvedValueOnce({ uuid: "session-v6", phase: "interviewing", turns: [openingTurn] });
    mocks.getSession.mockResolvedValueOnce(safetySession);
    mocks.submitTurnStream.mockImplementation(async (_uuid, _payload, onEvent) => {
      await onEvent({
        event: "agent_completed",
        data: {
          turn: safetySession.turns[2],
          session_action: "finish",
          finish_reason: "safety_stopped",
          session: safetySession,
        },
      });
    });

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("textarea").setValue("我现在有立即危险，已经准备做伤害自己的事，而且身边暂时没有人。");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain("本次对话已停止");
    expect(wrapper.text()).not.toContain("澄澄觉得这段对话已经自然收束");
    expect(mocks.finalizeSession).not.toHaveBeenCalled();
  });

  it("explains a transient model connection interruption and keeps the answer recoverable", async () => {
    mocks.submitTurnStream.mockImplementation(async (_uuid, _payload, onEvent) => {
      await onEvent({
        event: "error",
        code: "model_connection_interrupted",
        message: "与访谈模型的连接暂时中断，已保存你的回答。请重试。",
      });
    });

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("textarea").setValue("我正在等一个重要回复，也想把接下来需要确认的事情理清楚。 ");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain("与访谈模型的连接暂时中断，已保存你的回答。请重试。");
    expect(wrapper.text()).toContain("用原提交编号重试");
    expect(localStorage.getItem("v6:pending-turn:session-v6")).not.toBeNull();
  });

  it("shows saved and extended wait states at 8 and 20 seconds, then clears them", async () => {
    vi.useFakeTimers();
    let completeStream: (() => void) | undefined;
    mocks.submitTurnStream.mockImplementation(
      () => new Promise<void>((resolve) => {
        completeStream = resolve;
      }),
    );
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("textarea").setValue(validAnswer);
    await wrapper.get("form").trigger("submit");

    await vi.advanceTimersByTimeAsync(7_999);
    expect(wrapper.text()).not.toContain("本次提交已在本地保留");
    await vi.advanceTimersByTimeAsync(1);
    expect(wrapper.text()).toContain("正在整理这条回答；本次提交已在本地保留。");
    await vi.advanceTimersByTimeAsync(12_000);
    expect(wrapper.text()).toContain("仍在处理中；如果中断，可以使用原提交编号安全重试。");

    completeStream?.();
    await flushPromises();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(wrapper.text()).not.toContain("仍在处理中；如果中断");
    expect(vi.getTimerCount()).toBe(0);
    wrapper.unmount();
    vi.useRealTimers();
  });

  it("keeps a finalizing notice from being overwritten by answer wait timers", async () => {
    vi.useFakeTimers();
    mocks.submitTurnStream.mockImplementation(async (_uuid, _payload, onEvent) => {
      await onEvent({ event: "session_finalizing", data: {} });
      await new Promise<void>(() => undefined);
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("textarea").setValue(validAnswer);
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain("正在整理报告");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(wrapper.text()).toContain("正在整理报告");
    expect(wrapper.text()).not.toContain("仍在处理中");
    expect(vi.getTimerCount()).toBe(0);
    wrapper.unmount();
    vi.useRealTimers();
  });

  it("removes an expired wait notice when the stream later fails", async () => {
    vi.useFakeTimers();
    let rejectStream: ((reason?: unknown) => void) | undefined;
    mocks.submitTurnStream.mockImplementation(
      () => new Promise<void>((_resolve, reject) => {
        rejectStream = reject;
      }),
    );
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("textarea").setValue(validAnswer);
    await wrapper.get("form").trigger("submit");
    await vi.advanceTimersByTimeAsync(20_000);
    expect(wrapper.text()).toContain("仍在处理中");

    rejectStream?.(new Error("与访谈模型的连接暂时中断"));
    await flushPromises();
    expect(wrapper.text()).toContain("与访谈模型的连接暂时中断");
    expect(wrapper.text()).not.toContain("仍在处理中");
    expect(vi.getTimerCount()).toBe(0);
    wrapper.unmount();
    vi.useRealTimers();
  });

  it("shows an empty-model error without calling it a network interruption", async () => {
    mocks.submitTurnStream.mockImplementation(async (_uuid, _payload, onEvent) => {
      await onEvent({
        event: "error",
        code: "model_empty_response",
        message: "模型暂时未返回有效内容；你的回答已保存，可以安全重试。",
      });
    });

    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("textarea").setValue(validAnswer);
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain("模型暂时未返回有效内容");
    expect(wrapper.text()).not.toContain("连接暂时中断");
    expect(localStorage.getItem("v6:pending-turn:session-v6")).not.toBeNull();
  });

  it("clears pending wait timers when the page unmounts", async () => {
    vi.useFakeTimers();
    const clearTimeoutSpy = vi.spyOn(window, "clearTimeout");
    mocks.submitTurnStream.mockImplementation(() => new Promise<void>(() => undefined));
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("textarea").setValue(validAnswer);
    await wrapper.get("form").trigger("submit");

    wrapper.unmount();
    expect(clearTimeoutSpy.mock.calls.length).toBeGreaterThanOrEqual(2);
    vi.useRealTimers();
  });

  it("allows a nonblank short first answer and submits once with an unmodified Enter", async () => {
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    const textarea = wrapper.get("textarea");
    await textarea.setValue("我还在想。");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeUndefined();
    expect(wrapper.get(".char-count").text()).toBe("首次可简短；之后每次至少 20 字");
    expect(wrapper.get(".char-count").text()).not.toContain("4000");
    expect(textarea.attributes("aria-describedby")).toBe("answer-requirement");
    expect(textarea.attributes("placeholder")).not.toContain("至少 20 字");

    await textarea.trigger("keydown", { key: "Enter", shiftKey: true });
    await textarea.trigger("keydown", { key: "Enter", isComposing: true });
    await flushPromises();
    expect(mocks.submitTurnStream).not.toHaveBeenCalled();

    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.submitTurnStream).toHaveBeenCalledTimes(1);
    expect(mocks.submitTurnStream.mock.calls[0][1]).toMatchObject({ content: "我还在想。" });
  });

  it("keeps the later 20-character gate but exempts a complete uncertainty answer", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 1,
      turns: [
        openingTurn,
        { id: 2, turn_index: 1, role: "user", content: "先等等。" },
        { id: 3, turn_index: 2, role: "assistant", content: "你愿意从哪里继续说？" },
      ],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    const textarea = wrapper.get("textarea");
    expect(textarea.attributes("placeholder")).not.toContain("至少 20 字");
    await textarea.setValue("我还在想。");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeDefined();
    expect(wrapper.get(".char-count").text()).toMatch(/^至少 20 字，还差 \d+ 字$/);
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.submitTurnStream).not.toHaveBeenCalled();

    await textarea.setValue("我不知道，但我会先核实。");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeDefined();

    await textarea.setValue("我会先核实相关信息，再比较不同选择可能带来的具体影响和限制。");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeUndefined();
    expect(wrapper.get(".char-count").text()).toBe("可以提交");

    await textarea.setValue("我暂时不知道。");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeUndefined();
    expect(wrapper.get(".char-count").text()).toContain("可以直接提交");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.submitTurnStream).toHaveBeenCalledTimes(1);
    expect(mocks.submitTurnStream.mock.calls[0][1]).toMatchObject({ content: "我暂时不知道。" });
  });

  it("renders the current number of saved user answers without implying a target", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 2,
      turns: [
        openingTurn,
        { id: 2, turn_index: 1, role: "user", content: validAnswer },
        { id: 3, turn_index: 2, role: "assistant", content: "你最想先厘清的是什么？" },
        { id: 4, turn_index: 3, role: "user", content: "我也会继续核实不同选择可能带来的影响和限制。" },
      ],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.get(".round-count").text()).toBe("已进行 2 轮问答");
    expect(wrapper.text()).not.toContain("最多 2");
  });

  it("replaces the composer with a clear technical-limit handoff after 40 saved answers", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 40,
      technical_turn_cap: 40,
      technical_turn_cap_reached: true,
      turns: [openingTurn],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.get(".technical-limit-card").text()).toContain("技术保护上限");
    expect(wrapper.get(".technical-limit-card").text()).toContain("回答均已保存");
    expect(wrapper.get(".technical-limit-card").text()).toContain("不代表系统在判定证据已充分");
    expect(wrapper.find("textarea").exists()).toBe(false);
    expect(wrapper.find("button.send-button").exists()).toBe(false);

    await wrapper.get(".technical-limit-card button").trigger("click");
    await flushPromises();

    expect(mocks.finalizeSession).toHaveBeenCalledWith("session-v6");
    expect(mocks.replace).toHaveBeenCalledWith("/assessment/report/session-v6");
  });

  it("drops an expired local recovery answer instead of submitting it", async () => {
    localStorage.setItem("v6:pending-turn:session-v6", JSON.stringify({
      saved_at: Date.now() - (25 * 60 * 60 * 1000),
      payload: { content: "过期答案", client_turn_id: "expired-id", input_mode: "text", answer_duration_ms: 1000 },
    }));

    mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(mocks.submitTurnStream).not.toHaveBeenCalled();
    expect(localStorage.getItem("v6:pending-turn:session-v6")).toBeNull();
  });

  it("keeps a nonblank short pending answer recoverable", async () => {
    localStorage.setItem("v6:pending-turn:session-v6", JSON.stringify({
      saved_at: Date.now(),
      payload: { content: "不知道", client_turn_id: "pending-short-id", input_mode: "text", answer_duration_ms: 1000 },
    }));

    mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(mocks.submitTurnStream).toHaveBeenCalledTimes(1);
    expect(mocks.submitTurnStream.mock.calls[0][1]).toMatchObject({
      content: "不知道",
      client_turn_id: "pending-short-id",
    });
  });
});
