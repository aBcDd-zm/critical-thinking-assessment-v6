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
          observation: "只有一次可引用的用户原话。",
          suggestion: "补充核实来源。",
          evidences: [{ quote: "我会再看样本来源", source_type: "user", turn_index: 3 }],
        },
      },
    });
    expect(wrapper.text()).toContain("证据有限");
    expect(wrapper.text()).toContain("—");
    await wrapper.get("button").trigger("click");
    expect(wrapper.text()).toContain("本次观察");
    expect(wrapper.text()).not.toContain("优势");
    expect(wrapper.text()).toContain("我会再看样本来源");
    expect(wrapper.text()).toContain("第 3 次回答 · 用户原话");
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
          observation: "能区分事实与假设。",
          strength: "能区分事实与假设。",
          suggestion: "继续区分事实与假设。",
          evidences: [{ quote: "这是我的假设", source_type: "user" }],
        },
      },
    });
    expect(wrapper.text()).toContain("证据充分");
    expect(wrapper.text()).toContain("证据等级 4/5（序数）");
  });

  it("does not relabel a low evidence level as a strength", async () => {
    const wrapper = mount(DimensionCard, {
      props: {
        dimension: {
          dimension_key: "reasoning_argumentation",
          dimension_name: "推理与论证",
          status: "sufficient",
          score: 2,
          reason: "本次原话只支持二级观察。",
          observation: "本次原话只支持二级观察。",
          suggestion: "继续区分结论与依据。",
          evidences: [{ quote: "我只是先这样猜", source_type: "user" }],
        },
      },
    });
    await wrapper.get("button").trigger("click");
    expect(wrapper.text()).toContain("本次观察");
    expect(wrapper.text()).not.toContain("优势");
  });
});
