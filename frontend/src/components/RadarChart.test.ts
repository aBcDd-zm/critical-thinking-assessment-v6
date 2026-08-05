import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import RadarChart from "./RadarChart.vue";

describe("RadarChart", () => {
  it("uses status markers for unscored dimensions instead of plotting zero", () => {
    const wrapper = mount(RadarChart, {
      props: {
        dimensions: [
          { dimension_key: "problem_definition", dimension_name: "问题界定", status: "sufficient", score: 4, reason: "", suggestion: "", evidences: [] },
          { dimension_key: "evidence_evaluation", dimension_name: "证据评估", status: "limited", score: null, reason: "", suggestion: "", evidences: [] },
        ],
      },
    });
    expect(wrapper.findAll(".radar-score-dot")).toHaveLength(1);
    expect(wrapper.findAll(".radar-status-dot")).toHaveLength(5);
    expect(wrapper.get("svg").attributes("aria-label")).toContain("序数证据等级");
    expect(wrapper.html()).toContain("证据等级 4/5（序数）");
    expect(wrapper.find(".radar-area").exists()).toBe(false);
  });

  it("draws a radar area only when all six dimensions have sufficient evidence", () => {
    const dimensions = [
      "problem_definition",
      "evidence_evaluation",
      "reasoning_argumentation",
      "multiple_perspectives",
      "integrative_decision",
      "dynamic_adjustment",
    ].map((dimension_key) => ({
      dimension_key,
      dimension_name: dimension_key,
      status: "sufficient" as const,
      score: 4,
      reason: "",
      suggestion: "",
      evidences: [],
    }));
    const wrapper = mount(RadarChart, { props: { dimensions } });
    expect(wrapper.find(".radar-area").exists()).toBe(true);
  });
});
