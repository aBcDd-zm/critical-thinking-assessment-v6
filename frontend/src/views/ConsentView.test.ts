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

  it("shows concise task guidance, current completion rules, and explicit consent", async () => {
    const wrapper = mount(ConsentView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });

    expect(wrapper.get(".eyebrow").text()).toBe("批判性思维探索访谈");
    expect(wrapper.get("h1").text()).toBe("开始一次具体的思维访谈");
    expect(wrapper.get(".consent-intro p").text()).toContain("这不是与 AI 随意聊天");
    expect(wrapper.get(".consent-intro p").text()).toContain("亲身经历、需要判断、取舍或行动的具体事情");
    expect(wrapper.get(".assessment-guide").text()).toContain("开始前请了解");
    expect(wrapper.get(".assessment-guide").text()).toContain("没有标准答案");
    expect(wrapper.get(".assessment-guide").text()).toContain("至少需要 20 个非空白字符");
    expect(wrapper.get(".assessment-guide").text()).toContain("至少完成 8 轮回答");
    expect(wrapper.get(".assessment-guide").text()).toContain("现有证据足以支持完整报告");
    expect(wrapper.get(".assessment-guide").text()).toContain("是否生成报告由你确认");
    expect(wrapper.get(".assessment-guide").text()).toContain("证据有限");
    expect(wrapper.get(".assessment-guide").text()).toContain("探索性、非标准化访谈");
    expect(wrapper.text()).toContain("这不是与 AI 随意聊天");
    expect(wrapper.text()).not.toContain("内容初步谈清");
    expect(wrapper.text()).toContain("隐私摘要");
    expect(wrapper.text()).toContain("发送给 DeepSeek");
    expect(wrapper.text()).toContain("语音服务商豆包");
    expect(wrapper.text()).toContain("模型服务商与语音服务商侧的删除以其各自条款为准");
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
      consent_version: "v6-natural-interview-guidance-2026-08-v2",
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
