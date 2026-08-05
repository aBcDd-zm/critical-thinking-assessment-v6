import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/http";
import InterviewView from "./InterviewView.vue";

const mocks = vi.hoisted(() => ({
  getSession: vi.fn(),
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
    mocks.finalizeSession.mockResolvedValue({
      session: { uuid: "session-v6", phase: "completed", turns: [], report_available: true },
    });
  });

  it("shows the effective-answer target without exposing internal measurement routing", async () => {
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("访谈官 · 澄澄");
    expect(wrapper.get(".round-count").text()).toBe("有效回答 0/40");
    expect(wrapper.text()).toContain("完成 40 次后可生成报告");
    expect(wrapper.get("button.compact-action").attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("语音输入");
    expect(wrapper.text()).toContain("首次回答简短也可以");
    expect(wrapper.get("textarea").attributes("placeholder")).toContain("没听明白可点“换个问法”");
    expect(wrapper.find(".natural-interview-note").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("最多 12");
    expect(wrapper.text()).not.toContain("问题界定");
    expect(wrapper.text()).not.toContain("覆盖率");
    expect(wrapper.text()).not.toContain("自然访谈 · 实验性");
    expect(wrapper.text()).not.toContain("从你愿意说的地方开始就好");
    expect(wrapper.find(".progress-block").exists()).toBe(false);

    expect(mocks.finalizeSession).not.toHaveBeenCalled();
  });

  it("enables report generation only after 40 effective answers", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 40,
      turns: [openingTurn],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.get(".round-count").text()).toBe("有效回答 40/40 · 已达到完成条件");
    expect(wrapper.get("button.compact-action").attributes("disabled")).toBeUndefined();
    await wrapper.get("button.compact-action").trigger("click");
    await flushPromises();

    expect(mocks.finalizeSession).toHaveBeenCalledWith("session-v6");
    expect(mocks.replace).toHaveBeenCalledWith("/assessment/report/session-v6");
  });

  it("automatically requests a report after the interviewer naturally closes", async () => {
    const finishedSession = {
      uuid: "session-v6",
      phase: "finalizing",
      user_answer_count: 40,
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

  it.each(["不知道", "什么意思", "我想结束", "我想自杀"])("accepts the nonblank first answer %s with an unmodified Enter", async (answer) => {
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    const textarea = wrapper.get("textarea");
    await textarea.setValue(answer);
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeUndefined();
    expect(wrapper.get(".char-count").text()).toMatch(/首次回答简短也可以|澄清请求可直接提交|这类重要输入可直接提交/);
    await textarea.trigger("keydown", { key: "Enter", shiftKey: true });
    await textarea.trigger("keydown", { key: "Enter", isComposing: true });
    await flushPromises();
    expect(mocks.submitTurnStream).not.toHaveBeenCalled();

    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.submitTurnStream).toHaveBeenCalledTimes(1);
    expect(mocks.submitTurnStream.mock.calls[0][1]).toMatchObject({ content: answer });
  });

  it("requires 20 normalized visible characters after the first effective answer", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 1,
      turns: [openingTurn, { id: 2, turn_index: 1, role: "user", content: "工作" }],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    const textarea = wrapper.get("textarea");
    await textarea.setValue("😀✨！！！");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeDefined();
    expect(wrapper.get(".char-count").text()).toContain("0/20");

    await textarea.setValue("我不理解这个结论");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeDefined();
    expect(wrapper.get(".char-count").text()).not.toContain("澄清请求可直接提交");

    await textarea.setValue(`${"字".repeat(19)} ！！！`);
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeDefined();
    expect(wrapper.get(".char-count").text()).toContain("19/20");
    expect(wrapper.get(".char-count").text()).toContain("原因、依据或一个具体例子");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.submitTurnStream).not.toHaveBeenCalled();

    await textarea.setValue(`${"字".repeat(20)} ！！！`);
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeUndefined();
    expect(wrapper.get(".char-count").text()).toContain("20/20");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.submitTurnStream).toHaveBeenCalledTimes(1);
  });

  it.each([
    "我想轻生",
    "我准备吞药",
    "我要割腕",
    "我要伤害自己",
    "我想伤害我自己",
    "我要伤害你",
    "我想伤害别人",
    "我要伤害他人",
    "伤害其他人",
    "我想杀人",
    "正在打我",
    "我被绑架",
    "I want to die",
    "I might hurt someone",
  ])("never blocks the short immediate-risk input %s", async (answer) => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 1,
      turns: [openingTurn],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    const textarea = wrapper.get("textarea");
    await textarea.setValue(answer);
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeUndefined();
    expect(wrapper.get(".char-count").text()).toContain("这类重要输入可直接提交");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.submitTurnStream).toHaveBeenCalledTimes(1);
  });

  it.each([
    "这个决定可能伤害项目利益",
    "这种做法会伤害团队的长期利益",
    "我要伤害项目利益",
    "我要评估这项变更是否会伤害项目利益",
  ])("does not treat ordinary project-impact wording as an immediate-risk bypass: %s", async (answer) => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 1,
      turns: [openingTurn],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    const textarea = wrapper.get("textarea");
    await textarea.setValue(answer);
    expect(wrapper.get(".char-count").text()).not.toContain("这类重要输入可直接提交");
  });

  it.each([
    "我不想继续了",
    "不用再问了",
    "到这里吧",
    "先这样吧",
    "生成报告",
  ])("allows the short explicit-exit input %s without weakening the length gate", async (answer) => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 1,
      turns: [openingTurn],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    const textarea = wrapper.get("textarea");
    await textarea.setValue(answer);
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeUndefined();
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.submitTurnStream).toHaveBeenCalledTimes(1);
  });

  it("does not confuse a topic phrase with a request to exit", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 1,
      turns: [openingTurn],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    await wrapper.get("textarea").setValue("结束这个项目");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeDefined();
  });

  it("sends a clarification request independently without advancing the effective count", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 7,
      turns: [openingTurn],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();
    await wrapper.get("textarea").setValue("这段草稿还没写完");

    await wrapper.get(".conversation-shortcuts button").trigger("click");
    await flushPromises();

    expect(mocks.submitTurnStream).toHaveBeenCalledTimes(1);
    expect(mocks.submitTurnStream.mock.calls[0][1]).toMatchObject({
      content: "我没理解，请换一种问法。",
      interaction_kind: "clarification",
      input_mode: "text",
    });
    expect(wrapper.get("textarea").element.value).toBe("这段草稿还没写完");
    expect(wrapper.get(".round-count").text()).toBe("有效回答 7/40");
  });

  it("classifies a typed bounded clarification with ignored punctuation and emoji", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 7,
      turns: [openingTurn],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    const textarea = wrapper.get("textarea");
    await textarea.setValue("什么意思？😊");
    expect(wrapper.get(".char-count").text()).toContain("澄清请求可直接提交");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeUndefined();
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();

    expect(mocks.submitTurnStream).toHaveBeenCalledTimes(1);
    expect(mocks.submitTurnStream.mock.calls[0][1]).toMatchObject({
      content: "什么意思？😊",
      interaction_kind: "clarification",
    });
  });

  it("keeps the independent exit path report-free", async () => {
    mocks.exitSession.mockResolvedValue({ uuid: "session-v6", phase: "exited", turns: [] });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    await wrapper.get(".conversation-shortcuts button:last-child").trigger("click");
    await flushPromises();

    expect(mocks.exitSession).toHaveBeenCalledWith("session-v6");
    expect(mocks.finalizeSession).not.toHaveBeenCalled();
    expect(mocks.push).toHaveBeenCalledWith("/assessment");
  });

  it("turns a stale early-finalize response into a friendly completion reminder", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 40,
      turns: [openingTurn],
    });
    mocks.finalizeSession.mockRejectedValue(new ApiError("最低回答数未达到", 409, {
      code: "minimum_valid_answers_not_reached",
    }));
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    await wrapper.get("button.compact-action").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("服务端尚未确认完成条件");
  });

  it("routes to the report when a finalize timeout is followed by a completed sync", async () => {
    const completedSession = {
      uuid: "session-v6",
      phase: "completed",
      turns: [openingTurn],
      report_available: true,
    };
    mocks.getSession
      .mockResolvedValueOnce({ uuid: "session-v6", phase: "interviewing", user_answer_count: 40, turns: [openingTurn] })
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

  it("keeps a whitespace-only answer disabled", async () => {
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    const textarea = wrapper.get("textarea");
    await textarea.setValue("  \n ");
    expect(wrapper.get("button.send-button").attributes("disabled")).toBeDefined();
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.submitTurnStream).not.toHaveBeenCalled();
  });

  it("renders the server-authoritative effective-answer count", async () => {
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

    expect(wrapper.get(".round-count").text()).toBe("有效回答 2/40");
  });

  it("prefers the V6.1 authoritative protocol fields over the compatibility alias", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      user_answer_count: 40,
      valid_answer_count: 6,
      interview_protocol_version: "natural_interviewer_v6.1",
      minimum_valid_answers: 42,
      maximum_user_answers: 45,
      remaining_required_answers: 36,
      can_finalize: false,
      turns: [openingTurn],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.get(".round-count").text()).toBe("有效回答 6/42");
    expect(wrapper.get("button.compact-action").attributes("disabled")).toBeDefined();
    expect(wrapper.get("button.compact-action").text()).toContain("完成 42 次后可生成报告");
  });

  it("uses only explicitly valid turns for a conservative legacy fallback", async () => {
    mocks.getSession.mockResolvedValue({
      uuid: "session-v6",
      phase: "interviewing",
      turns: [
        openingTurn,
        { id: 2, turn_index: 1, role: "user", content: validAnswer, quality_flags: ["valid_answer"] },
        { id: 3, turn_index: 2, role: "assistant", content: "你还想澄清哪一点？" },
        { id: 4, turn_index: 3, role: "user", content: "我没理解，请换一种问法。", quality_flags: ["clarification_request"] },
        { id: 5, turn_index: 4, role: "user", content: "旧客户端未携带分类标记" },
      ],
    });
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.get(".round-count").text()).toBe("有效回答 1/40");
    expect(wrapper.get("button.compact-action").attributes("disabled")).toBeDefined();
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
});
