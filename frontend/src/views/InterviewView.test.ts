import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import InterviewView from "./InterviewView.vue";

const mocks = vi.hoisted(() => ({
  getSession: vi.fn(),
  checkReportReadiness: vi.fn(),
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

  it("automatically requests a report after the interviewer naturally closes", async () => {
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

  it("allows a nonblank short first answer and submits once with an unmodified Enter", async () => {
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    const textarea = wrapper.get("textarea");
    await textarea.setValue("我还在想。");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeUndefined();
    expect(wrapper.get(".char-count").text()).toContain("首次回答可以简短");
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
    expect(textarea.attributes("placeholder")).toContain("至少 20 字");
    await textarea.setValue("我还在想。");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeDefined();
    expect(wrapper.get(".char-count").text()).toContain("还差");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.submitTurnStream).not.toHaveBeenCalled();

    await textarea.setValue("我不知道，但我会先核实。");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeDefined();

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
