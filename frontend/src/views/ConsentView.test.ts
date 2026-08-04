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

  it("keeps the entry page to essential consent and start controls", async () => {
    const wrapper = mount(ConsentView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });

    expect(wrapper.get("h1").text()).toBe("开始访谈");
    expect(wrapper.text()).toContain("隐私与使用说明");
    expect(wrapper.find("details").attributes("open")).toBeUndefined();
    expect(wrapper.get("input[autocomplete=username]").attributes("required")).toBeDefined();
    expect(wrapper.text()).not.toContain("自然访谈实验");
    expect(wrapper.text()).not.toContain("复核工作台");
    expect(wrapper.text()).not.toContain("没有固定题单、阶段或答题次数");
    expect(wrapper.text()).not.toContain("真实议题");
    expect(wrapper.text()).not.toContain("8–12");
    expect(wrapper.get("button[type=submit]").attributes("disabled")).toBeDefined();
    expect(wrapper.find("[name=occupation]").exists()).toBe(false);

    await wrapper.get("input[autocomplete=username]").setValue("本地统计样本");
    await wrapper.get("input[type=checkbox]").setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(mocks.createSession).toHaveBeenCalledWith({
      consent_version: "v6-natural-interview-2026-08",
      consent_given: true,
      participant: { display_name: "本地统计样本" },
    });
    const request = mocks.createSession.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(JSON.stringify(request)).not.toContain("occupation");
    expect(JSON.stringify(request)).not.toContain("identity_type");
    expect(localStorage.getItem("v6:last-session")).toBe("session-v6");
    expect(mocks.push).toHaveBeenCalledWith("/assessment/session/session-v6");
  });
});
