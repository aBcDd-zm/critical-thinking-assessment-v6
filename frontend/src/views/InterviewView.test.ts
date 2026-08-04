import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
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

  it("shows a single natural interviewer with no public stages, coverage, or answer quota", async () => {
    const wrapper = mount(InterviewView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("访谈官 · 澄澄");
    expect(wrapper.text()).toContain("结束并生成报告");
    expect(wrapper.text()).toContain("语音输入");
    expect(wrapper.find(".natural-interview-note").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("最多 12");
    expect(wrapper.text()).not.toContain("问题界定");
    expect(wrapper.text()).not.toContain("覆盖率");
    expect(wrapper.text()).not.toContain("自然访谈 · 实验性");
    expect(wrapper.text()).not.toContain("从你愿意说的地方开始就好");
    expect(wrapper.find(".progress-block").exists()).toBe(false);

    await wrapper.get("button.compact-action").trigger("click");
    await flushPromises();

    expect(mocks.finalizeSession).toHaveBeenCalledWith("session-v6");
    expect(mocks.replace).toHaveBeenCalledWith("/assessment/report/session-v6");
  });

  it("automatically requests a report after the interviewer naturally closes", async () => {
    const finishedSession = {
      uuid: "session-v6",
      phase: "finalizing",
      user_answer_count: 1,
      turns: [
        openingTurn,
        { id: 2, turn_index: 1, role: "user" as const, content: "我还在想下一步。", client_turn_id: "client-natural-finish", input_mode: "text", answer_duration_ms: 100 },
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
    await wrapper.get("textarea").setValue("我还在想下一步。");
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
        { id: 2, turn_index: 1, role: "user" as const, content: "我现在有立即危险。", client_turn_id: "client-safety-stop", input_mode: "text", answer_duration_ms: 100 },
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
    await wrapper.get("textarea").setValue("我现在有立即危险。");
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
    await wrapper.get("textarea").setValue("我正在等一个回复。 ");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain("与访谈模型的连接暂时中断，已保存你的回答。请重试。");
    expect(wrapper.text()).toContain("用原提交编号重试");
    expect(localStorage.getItem("v6:pending-turn:session-v6")).not.toBeNull();
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
