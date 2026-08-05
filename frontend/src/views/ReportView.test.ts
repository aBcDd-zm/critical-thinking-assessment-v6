import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ReportView from "./ReportView.vue";

const mocks = vi.hoisted(() => ({
  getReport: vi.fn(),
  getReportPdf: vi.fn(),
}));

vi.mock("@/api/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/session")>();
  return { ...actual, getReport: mocks.getReport, getReportPdf: mocks.getReportPdf };
});
vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { sessionUuid: "session-v6" } }),
}));

describe("ReportView", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mocks.getReport.mockResolvedValue({
      session_uuid: "session-v6",
      summary: "只呈现本次自然访谈中可追溯的用户原话。",
      strengths: ["能区分事实与假设"],
      priorities: ["记录改变原判断的条件"],
      dimensions: [
        { dimension_key: "problem_definition", dimension_name: "问题界定", status: "sufficient", score: 4, reason: "有精确原话。", observation: "能区分事实与假设。", strength: "能区分事实与假设。", suggestion: "继续标记假设。", evidences: [{ quote: "这只是我现在的假设", source_type: "user", turn_index: 2 }] },
        { dimension_key: "evidence_evaluation", dimension_name: "证据评估", status: "limited", score: null, reason: "证据较少。", observation: "证据较少。", suggestion: "增加核实来源。", evidences: [] },
      ],
      experimental_notice: "实验结果不用于人格或职业判断。",
      manual_review_recommended: true,
      disclaimer: "不替代专业决定。",
    });
  });

  it("keeps the report concise without calculating or presenting a composite score", async () => {
    const wrapper = mount(ReportView, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    await flushPromises();

    expect(wrapper.get("h1").text()).toBe("访谈结果");
    expect(wrapper.find(".radar-chart").exists()).toBe(true);
    expect(wrapper.text()).toContain("证据等级 4/5（序数）");
    expect(wrapper.text()).toContain("证据充分");
    expect(wrapper.text()).toContain("证据有限");
    expect(wrapper.text()).toContain("未充分测得");
    expect(wrapper.find(".coverage-number").exists()).toBe(false);
    expect(wrapper.find(".overall-score").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("置信度");
    expect(wrapper.text()).not.toContain("综合总分");
    expect(wrapper.text()).not.toContain("基于 1 个证据充分维度计算");
    expect(wrapper.text()).not.toContain("自然访谈观察报告");
    expect(wrapper.text()).not.toContain("下一次可以留意的方向");
    expect(wrapper.text()).not.toContain("80 分");

    await wrapper.findAll(".dimension-head")[0]!.trigger("click");
    expect(wrapper.text()).toContain("本次观察");
    expect(wrapper.text()).toContain("能区分事实与假设");
    expect(wrapper.text()).toContain("这只是我现在的假设");
    expect(wrapper.text()).toContain("第 2 次回答 · 用户原话");
    expect(wrapper.text()).toContain("优势");
    expect(wrapper.text()).toContain("建议");
  });
});
