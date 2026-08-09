import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import DimensionCard from "./DimensionCard.vue";

describe("DimensionCard", () => {
  it("shows evidence status instead of manufacturing a score", async () => {
    const wrapper = mount(DimensionCard, {
      props: {
        dimension: {
          dimension_key: "evidence_evaluation",
          dimension_name: "证据评估",
          status: "limited",
          score: null,
          reason: "只有一次可引用的用户原话。",
          suggestion: "补充核实来源。",
          evidences: [{ quote: "我会再看样本来源", source_type: "user", turn_index: 3, answer_ordinal: 2 }],
        },
      },
    });
    expect(wrapper.text()).toContain("证据有限");
    expect(wrapper.text()).toContain("—");
    await wrapper.get("button").trigger("click");
    expect(wrapper.text()).toContain("优势");
    expect(wrapper.text()).toContain("我会再看样本来源");
    expect(wrapper.text()).toContain("第 2 次回答 · 用户原话");
    expect(wrapper.text()).toContain("建议");
    expect(wrapper.text()).toContain("补充核实来源");
  });

  it("shows a score only when evidence is sufficient", () => {
    const wrapper = mount(DimensionCard, {
      props: {
        dimension: {
          dimension_key: "problem_definition",
          dimension_name: "问题界定",
          status: "sufficient",
          score: 4,
          reason: "有两条精确原话。",
          suggestion: "继续区分事实与假设。",
          evidences: [{ quote: "这是我的假设", source_type: "user" }],
        },
      },
    });
    expect(wrapper.text()).toContain("证据充分");
    expect(wrapper.text()).toContain("80 分");
    expect(wrapper.text()).not.toContain("4/5");
  });

  it("falls back to the raw dialogue coordinate without calling it an answer ordinal", async () => {
    const wrapper = mount(DimensionCard, {
      props: {
        dimension: {
          dimension_key: "problem_definition",
          dimension_name: "问题界定",
          status: "sufficient",
          score: 4,
          reason: "有精确原话。",
          suggestion: "继续核对。",
          evidences: [{ quote: "这是我的判断", source_type: "user", turn_index: 15 }],
        },
      },
    });

    await wrapper.get("button").trigger("click");
    expect(wrapper.text()).toContain("对话记录 #15 · 用户原话");
    expect(wrapper.text()).not.toContain("第 15 次回答");
  });
});
