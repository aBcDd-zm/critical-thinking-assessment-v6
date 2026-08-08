import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ConsentView from "./ConsentView.vue";

const mocks = vi.hoisted(() => ({
  createSession: vi.fn(),
  push: vi.fn(),
}));

vi.mock("@/api/session", () => ({ createSession: mocks.createSession }));
vi.mock("vue-router", () => ({
  useRouter: () => ({ push: mocks.push }),
}));

describe("ConsentView", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mocks.createSession.mockResolvedValue({
      session: { uuid: "session-v6", phase: "interviewing", turns: [] },
    });
  });

  it("shows only the two-line task guidance and explicit consent", async () => {
    const wrapper = mount(ConsentView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });

    expect(wrapper.get("h1").text()).toBe("在访谈对话中展现你的思维");
    expect(wrapper.get(".consent-intro p").text()).toBe("请从一件真实、具体、需要判断的经历说起");
    expect(wrapper.find(".eyebrow").exists()).toBe(false);
    expect(wrapper.find(".assessment-guide").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("这不是与 AI 随意聊天");
    expect(wrapper.text()).not.toContain("开始前请了解");
    expect(wrapper.text()).not.toContain("内容初步谈清");
    expect(wrapper.text()).toContain("隐私摘要");
    expect(wrapper.text()).toContain("发送给已配置的模型服务");
    expect(wrapper.text()).toContain("获授权的管理人员可能查看完整逐字稿");
    expect(wrapper.find("details").attributes("open")).toBeUndefined();
    expect(wrapper.get("input[autocomplete=off]").attributes("required")).toBeDefined();
    expect(wrapper.get("input[type=checkbox]").attributes("required")).toBeDefined();
    expect(wrapper.text()).not.toContain("复核工作台");
    expect(wrapper.text()).not.toContain("问题界定");
    expect(wrapper.text()).not.toContain("证据评估");
    expect(wrapper.text()).not.toContain("经科学验证");
    expect(wrapper.text()).not.toContain("8–12");
    expect(wrapper.get("button[type=submit]").attributes("disabled")).toBeDefined();
    expect(wrapper.find("[name=occupation]").exists()).toBe(false);

    await wrapper.get("input[autocomplete=off]").setValue("参与编号-07");
    await wrapper.get("input[type=checkbox]").setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(mocks.createSession).toHaveBeenCalledWith({
      consent_version: "v6-natural-interview-guidance-2026-08",
      consent_given: true,
      participant: { display_name: "参与编号-07" },
    });
    const request = mocks.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(JSON.stringify(request)).not.toContain("occupation");
    expect(JSON.stringify(request)).not.toContain("identity_type");
    expect(localStorage.getItem("v6:last-session")).toBe("session-v6");
    expect(mocks.push).toHaveBeenCalledWith("/assessment/session/session-v6");
  });

  it("offers the saved session before a participant starts another one", () => {
    localStorage.setItem("v6:last-session", "saved-session");

    const wrapper = mount(ConsentView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });

    expect(wrapper.get(".resume-banner").text()).toContain("检测到一段未完成的访谈");
    expect(wrapper.get(".resume-banner").text()).toContain("继续上次访谈");
  });
});
