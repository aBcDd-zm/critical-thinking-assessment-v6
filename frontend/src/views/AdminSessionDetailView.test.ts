import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AdminSessionDetailView from "./AdminSessionDetailView.vue";

const mocks = vi.hoisted(() => ({
  getAdminSession: vi.fn(),
  finalizeAdminSession: vi.fn(),
  saveExpertScores: vi.fn(),
  updateReview: vi.fn(),
}));

vi.mock("@/api/admin", () => ({
  getAdminSession: mocks.getAdminSession,
  finalizeAdminSession: mocks.finalizeAdminSession,
  saveExpertScores: mocks.saveExpertScores,
  updateReview: mocks.updateReview,
}));
vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { sessionUuid: "admin-session-v621" } }),
}));
vi.mock("@/composables/useAdminAuth", () => ({
  useAdminAuth: () => ({
    user: { value: { username: "reviewer", display_name: "复核员" } },
  }),
}));

const baseDetail = {
  uuid: "admin-session-v621",
  phase: "completed" as const,
  participant: { display_name: "参与者 07" },
  user_answer_count: 1,
  turns: [
    { id: 1, turn_index: 0, role: "assistant" as const, content: "当时你最难判断的是什么？" },
    { id: 2, turn_index: 1, role: "user" as const, content: "我想先确认自己真正重视什么。论文说要最大化期望效用。" },
  ],
  traces: [],
  scoring_runs: [],
  expert_scores: [],
  technical_anomalies: [],
  report: null,
};

async function mountInterviewTab() {
  const wrapper = mount(AdminSessionDetailView, {
    global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
  });
  await flushPromises();
  const interviewTab = wrapper.findAll(".tab-bar button").find((button) => button.text() === "访谈与证据");
  expect(interviewTab).toBeDefined();
  await interviewTab!.trigger("click");
  return wrapper;
}

describe("AdminSessionDetailView evidence attribution", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the current backend attribution and finalized evidence wire", async () => {
    mocks.getAdminSession.mockResolvedValue({
      ...baseDetail,
      evidence_attributions: [
        {
          id: 11,
          span_id: 11,
          user_turn_id: 2,
          readiness_check_id: 3,
          turn_index: 1,
          quote: "我想先确认自己真正重视什么",
          start: 0,
          end: 16,
          text_hash: "hash-participant",
          owner: "participant_owned",
          relation: "own_reasoning",
          source_label: "本人反思",
          elicitation_level: "open_probe",
          confidence: 0.96,
          eligibility: "eligible",
          validation_status: "validated",
          validation_reason: null,
          final_scoring_dimension_keys: ["problem_definition"],
          snapshot_used_dimension_keys: ["problem_definition"],
          used_dimension_keys: ["problem_definition"],
          eliciting_question: "当时你最难判断的是什么？",
          transcript_fingerprint: "transcript-v621",
          asset_fingerprint: "asset-v621",
          prompt_template_id: "evidence_attribution_v621",
          prompt_version: "6.2.1",
          schema_version: "evidence_attribution.v1",
        },
        {
          id: 12,
          span_id: 12,
          user_turn_id: 2,
          readiness_check_id: 3,
          turn_index: 1,
          quote: "论文说要最大化期望效用",
          start: 17,
          end: 31,
          text_hash: "hash-external",
          owner: "external_quoted",
          relation: "quotes_only",
          source_label: "论文",
          elicitation_level: "strong_scaffold",
          confidence: 0.91,
          eligibility: "context_only",
          validation_status: "rejected",
          validation_reason: "外部原文且未表达本人的取舍理由",
          final_scoring_dimension_keys: [],
          snapshot_used_dimension_keys: [],
          used_dimension_keys: [],
          eliciting_question: "当时你最难判断的是什么？",
          transcript_fingerprint: "transcript-v621",
          asset_fingerprint: "asset-v621",
          prompt_template_id: "evidence_attribution_v621",
          prompt_version: "6.2.1",
          schema_version: "evidence_attribution.v1",
        },
      ],
      evidence_items: [
        {
          dimension_key: "problem_definition",
          quote: "我想先确认自己真正重视什么",
          turn_index: 1,
          source_type: "user",
          status: "sufficient",
          active_for_scoring: true,
          quote_start: 0,
          quote_end: 16,
          confidence: 0.88,
          attribution_span_id: 11,
          readiness_check_id: 3,
          validation_status: "validated",
          validation_reason: null,
        },
      ],
    });

    const wrapper = await mountInterviewTab();
    const text = wrapper.text();

    expect(text).toContain("输入片段 / 归属");
    expect(text).toContain("当时你最难判断的是什么？");
    expect(text).toContain("参与者本人");
    expect(text).toContain("外部材料（原文引用）");
    expect(text).toContain("开放追问");
    expect(text).toContain("强提示");
    expect(text).toContain("归属置信度");
    expect(text).toContain("0.96");
    expect(text).toContain("终评已采用：问题界定");
    expect(text).toContain("未用于评分：归属校验已拒绝");
    expect(text).toContain("外部原文且未表达本人的取舍理由");
    expect(text).toContain("来源标签：论文");
    expect(text).toContain("归属 span #11");
    expect(text).toContain("字符 [0, 16) · 证据置信度 0.88");
    expect(text).toContain("用于终评");
    expect(wrapper.find("mark.attribution-span.participant_owned").text()).toBe("我想先确认自己真正重视什么");
    expect(wrapper.find("mark.attribution-span.external_quoted").text()).toBe("论文说要最大化期望效用");
    expect(text).not.toContain("终评引用的用户原话");
  });

  it("separates pre-final snapshot references from final scoring", async () => {
    mocks.getAdminSession.mockResolvedValue({
      ...baseDetail,
      phase: "interviewing",
      report_available: false,
      evidence_attributions: [
        {
          id: 21,
          span_id: 21,
          readiness_check_id: 4,
          turn_index: 1,
          quote: "我会先检查这条证据的来源",
          start: 0,
          end: 15,
          owner: "participant_owned",
          relation: "own_reasoning",
          elicitation_level: "spontaneous",
          confidence: 0.93,
          eligibility: "eligible",
          validation_status: "validated",
          final_scoring_dimension_keys: [],
          snapshot_used_dimension_keys: ["evidence_evaluation"],
          used_dimension_keys: [],
          eliciting_question: "你会怎么判断这条材料是否可靠？",
        },
        {
          id: 22,
          span_id: 22,
          readiness_check_id: 4,
          turn_index: 1,
          quote: "我还需要比较其他方案",
          start: 16,
          end: 27,
          owner: "participant_owned",
          relation: "own_reasoning",
          elicitation_level: "open_probe",
          confidence: 0.89,
          eligibility: "eligible",
          validation_status: "validated",
          final_scoring_dimension_keys: [],
          snapshot_used_dimension_keys: [],
          used_dimension_keys: [],
          eliciting_question: "你会怎么判断这条材料是否可靠？",
        },
      ],
      evidence_items: [],
    });

    const wrapper = await mountInterviewTab();
    const text = wrapper.text();

    expect(text).toContain("当前证据快照引用：证据评估");
    expect(text).toContain("候选片段未被终评采用");
    expect(text).not.toContain("终评已采用");
    expect(text).not.toContain("已用于评分");
  });

  it("treats legacy used_dimension_keys as snapshot-only before finalization", async () => {
    mocks.getAdminSession.mockResolvedValue({
      ...baseDetail,
      phase: "interviewing",
      report_available: false,
      evidence_attributions: [
        {
          id: 31,
          turn_index: 1,
          quote: "我会先界定问题",
          start: 0,
          end: 8,
          owner: "participant_owned",
          relation: "own_reasoning",
          elicitation_level: "open_probe",
          eligibility_status: "eligible",
          validation_status: "validated",
          used_dimension_keys: ["problem_definition"],
        },
      ],
      evidence_items: [],
    });

    const wrapper = await mountInterviewTab();
    const text = wrapper.text();

    expect(text).toContain("当前证据快照引用：问题界定");
    expect(text).not.toContain("终评已采用");
    expect(text).not.toContain("已用于评分");
  });

  it("keeps legacy admin responses without evidence_attributions readable", async () => {
    mocks.getAdminSession.mockResolvedValue({
      ...baseDetail,
      evidence_items: [
        {
          dimension_key: "integrative_decision",
          quote: "我会比较各个方案再做选择",
          turn_index: 1,
          source_type: "user",
          status: "limited",
        },
      ],
    });

    const wrapper = await mountInterviewTab();
    const text = wrapper.text();

    expect(text).toContain("暂无独立证据归属记录（旧合同或未启用）");
    expect(text).toContain("我会比较各个方案再做选择");
    expect(text).toContain("参与者输入");
    expect(text).not.toContain("undefined");
  });
});
