import { expect, test, type Page, type Route } from "@playwright/test";

const UUID = "e2e-v6-session-0001";

type Phase = "interviewing" | "finalizing" | "completed" | "exited" | "safety_stopped";
type MockState = {
  phase: Phase;
  answers: number;
  turns: Array<Record<string, unknown>>;
  payloads: Array<Record<string, unknown>>;
  finalizePayloads: Array<Record<string, unknown>>;
  persistedClientIds: Set<string>;
  technicalTurnCap: number | null;
  forceInsufficientReadiness: boolean;
  closureSuggestion: null | {
    closure_turn_id: number;
    transcript_fingerprint: string;
    finish_reason: "natural_closure";
  };
};

const INITIAL_QUESTION = "请想起最近一件真实、具体、需要认真权衡的事情：当时最难判断的是什么？";
const CAP_THANK_YOU = "谢谢你认真完成了本次访谈的 40 次回答。为保障系统稳定，本次访谈已达到技术保护上限，我将不再继续提问。你已提交的内容均已保存；达到上限本身不代表你的回答不充分、质量不高或能力不足。很抱歉本次不能继续接收回答，感谢你的投入与理解。";

function technicalCapTurns() {
  const turns: Array<Record<string, unknown>> = [
    { id: 1, turn_index: 0, role: "assistant", phase: "interviewing", content: INITIAL_QUESTION },
  ];
  for (let answer = 1; answer <= 40; answer += 1) {
    turns.push({
      id: turns.length + 1,
      turn_index: turns.length,
      role: "user",
      phase: "interviewing",
      content: `第${answer}次回答：我会核对信息、比较不同选择，并说明什么新反馈会改变我的判断。`,
      client_turn_id: `cap-answer-${answer}`,
      input_mode: "text",
    });
    turns.push({
      id: turns.length + 1,
      turn_index: turns.length,
      role: "assistant",
      phase: "interviewing",
      content: answer === 40 ? CAP_THANK_YOU : `这是第 ${answer} 次回答后的追问。`,
    });
  }
  return turns;
}

function transcriptFingerprint(state: MockState) {
  return String(Math.max(1, state.answers)).repeat(64).slice(0, 64);
}

function snapshot(state: MockState) {
  return {
    uuid: UUID,
    phase: state.phase,
    participant: { display_name: "小林" },
    user_answer_count: state.answers,
    turns: state.turns,
    report_available: state.phase === "completed",
    manual_review_recommended: false,
    closure_suggestion: state.closureSuggestion,
    ...(state.technicalTurnCap === null ? {} : {
      technical_turn_cap: state.technicalTurnCap,
      technical_turn_cap_reached: state.answers >= state.technicalTurnCap,
    }),
  };
}

function report() {
  const names = ["问题界定", "证据评估", "推理与论证", "多元视角", "综合决策", "动态调整"];
  const keys = ["problem_definition", "evidence_evaluation", "reasoning_argumentation", "multiple_perspectives", "integrative_decision", "dynamic_adjustment"];
  return {
    session_uuid: UUID,
    summary: "报告只呈现本次自然访谈中能回到用户原话的观察。",
    strengths: ["能说明自己当前的顾虑"],
    priorities: ["继续记录会改变判断的条件"],
    dimensions: keys.map((dimension_key, index) => ({
      dimension_key,
      dimension_name: names[index],
      status: index === 0 ? "sufficient" : index === keys.length - 1 ? "unmeasured" : "limited",
      score: index === 0 ? 4 : null,
      reason: index === 0 ? "有精确的用户原话。" : index === keys.length - 1 ? "未充分测得。" : "证据有限。",
      suggestion: "在相似情境中记录依据和改变条件。",
      evidences: index === 0 ? [{ quote: "我想先确认自己真正重视什么", source_type: "user", turn_index: 3 }] : [],
    })),
    experimental_notice: "实验性、非标准化访谈。",
    disclaimer: "不生成综合总分或人格判断。",
  };
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installMockBackend(page: Page, options: { atTechnicalCap?: boolean } = {}) {
  const atTechnicalCap = options.atTechnicalCap === true;
  const state: MockState = {
    phase: "interviewing",
    answers: atTechnicalCap ? 40 : 0,
    payloads: [],
    finalizePayloads: [],
    persistedClientIds: new Set(),
    technicalTurnCap: atTechnicalCap ? 40 : null,
    forceInsufficientReadiness: atTechnicalCap,
    closureSuggestion: null,
    turns: atTechnicalCap
      ? technicalCapTurns()
      : [{ id: 1, turn_index: 0, role: "assistant", phase: "interviewing", content: INITIAL_QUESTION }],
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/, "");
    if (path === "/sessions" && request.method() === "POST") {
      await fulfillJson(route, { session: snapshot(state), initial_turn: state.turns[0] }, 201);
      return;
    }
    if (path === `/sessions/${UUID}` && request.method() === "GET") {
      await fulfillJson(route, snapshot(state));
      return;
    }
    if (path === `/sessions/${UUID}/exit` && request.method() === "POST") {
      state.phase = "exited";
      await fulfillJson(route, snapshot(state));
      return;
    }
    if (path === `/sessions/${UUID}/turns:stream` && request.method() === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      state.payloads.push(payload);
      const clientId = String(payload.client_turn_id);
      if (!state.persistedClientIds.has(clientId)) {
        state.persistedClientIds.add(clientId);
        state.answers += 1;
        state.turns.push({ id: state.turns.length + 1, turn_index: state.turns.length, role: "user", phase: "interviewing", ...payload });
      }
      const assistant = {
        id: state.turns.length + 1,
        turn_index: state.turns.length,
        role: "assistant",
        phase: "interviewing",
        content: state.answers >= 2
          ? "我们再把这次经历往深处看一点：还有哪条重要依据、权衡或变化，是你觉得没有说清的？"
          : "听起来这对你很重要；你现在最在意的是什么？",
      };
      state.turns.push(assistant);
      const events = [
        { event: "user_turn_saved", data: { turn: state.turns.at(-2) } },
        { event: "agent_started", data: {} },
        { event: "agent_delta", delta: assistant.content },
        { event: "agent_completed", data: { turn: assistant, session_action: "continue", finish_reason: null, session: snapshot(state) } },
      ];
      await route.fulfill({
        status: 200,
        contentType: "application/x-ndjson",
        body: events.map((event) => JSON.stringify(event)).join("\n") + "\n",
      });
      return;
    }
    if (path === `/sessions/${UUID}/report-readiness` && request.method() === "POST") {
      const ready = !state.forceInsufficientReadiness && state.answers >= 8;
      await fulfillJson(route, {
        status: ready ? "ready" : "insufficient",
        ready,
        cached: true,
        check_id: state.answers,
        transcript_fingerprint: transcriptFingerprint(state),
        minimum_turns_required: 8,
        minimum_turns_met: state.answers >= 8,
      });
      return;
    }
    if (
      state.closureSuggestion
      && path === `/sessions/${UUID}/closure-suggestions/${state.closureSuggestion.closure_turn_id}/accept`
      && request.method() === "POST"
    ) {
      state.phase = "completed";
      state.closureSuggestion = null;
      await fulfillJson(route, { session: snapshot(state), report: report() });
      return;
    }
    if (path === `/sessions/${UUID}/finalize` && request.method() === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      state.finalizePayloads.push(payload);
      if (
        payload.evidence_check_id !== state.answers
        || payload.expected_transcript_fingerprint !== transcriptFingerprint(state)
        || payload.allow_incomplete !== state.forceInsufficientReadiness
      ) {
        await fulfillJson(route, { detail: "snapshot mismatch" }, 409);
        return;
      }
      state.phase = "completed";
      await fulfillJson(route, { session: snapshot(state), report: report() });
      return;
    }
    if (path === `/sessions/${UUID}/report` && request.method() === "GET") {
      await fulfillJson(route, report());
      return;
    }
    if (path === `/sessions/${UUID}/report.pdf` && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/pdf", body: "%PDF-1.4\n% V6 local test\n" });
      return;
    }
    await fulfillJson(route, { detail: `unhandled ${request.method()} ${path}` }, 404);
  });
  return state;
}

async function startInterview(page: Page) {
  await page.goto("/assessment");
  await expect(page.getByRole("heading", { name: "开始一次具体的思维访谈" })).toBeVisible();
  await expect(page.getByText(/这不是与 AI 随意聊天/)).toBeVisible();
  await page.getByLabel("参与编号或昵称").fill("本地流程验收");
  await page.getByLabel(/我已阅读并理解/).check();
  await page.getByRole("button", { name: /开始访谈/ }).click();
  await expect(page).toHaveURL(new RegExp(`/assessment/session/${UUID}`));
  await expect(page.getByText(INITIAL_QUESTION)).toBeVisible();
}

test("consent → natural conversation → evidence-ready snapshot → report", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("v6:tts-enabled", "false"));
  const state = await installMockBackend(page);
  await startInterview(page);

  await expect(page.getByText("第 1 / 最多 12 次回答")).toHaveCount(0);
  await expect(page.getByText("问题界定", { exact: true })).toHaveCount(0);
  await expect(page.getByText("已进行 0 轮问答", { exact: true })).toBeVisible();
  await page.getByLabel("你的回答").fill("我还在想。");
  await expect(page.getByRole("button", { name: /提交回答/ })).toBeEnabled();
  await expect(page.getByText("首次可简短；之后每次至少 20 字", { exact: true })).toBeVisible();
  await page.getByLabel("你的回答").press("Enter");
  await expect(page.getByText("听起来这对你很重要；你现在最在意的是什么？")).toBeVisible();
  await expect(page.getByText("已进行 1 轮问答", { exact: true })).toBeVisible();

  await page.getByLabel("你的回答").fill("我再想想。");
  await expect(page.getByRole("button", { name: /提交回答/ })).toBeDisabled();
  await expect(page.getByText(/还差 \d+ 字/)).toBeVisible();
  for (let answer = 2; answer <= 7; answer += 1) {
    await page.getByLabel("你的回答").fill(`第${answer}次回答：我想继续核对资料来源、不同人的考虑和可能改变判断的条件。`);
    await page.getByLabel("你的回答").press("Enter");
    await expect(page.getByText(`已进行 ${answer} 轮问答`, { exact: true })).toBeVisible();
    await expect(page.getByText("现有回答已足够生成完整报告", { exact: true })).toHaveCount(0);
  }
  await page.getByLabel("你的回答").fill("第8次回答：我会比较各个方案，说明优先级，并在新反馈出现时调整行动。");
  await page.getByLabel("你的回答").press("Enter");
  await expect(page.getByText("现有回答已足够生成完整报告", { exact: true })).toBeVisible();
  await expect(page.getByText("这段对话可以在这里收束", { exact: true })).toHaveCount(0);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "结束并生成报告" }).click();
  await expect(page).toHaveURL(new RegExp(`/assessment/report/${UUID}$`));
  await expect(page.getByRole("heading", { name: "访谈结果" })).toBeVisible();
  await expect(page.locator(".radar-chart")).toBeVisible();
  await expect(page.getByText("综合总分", { exact: true })).toBeVisible();
  expect(state.answers).toBe(8);
  expect(state.payloads).toHaveLength(8);
  expect(state.payloads.every((payload) => !JSON.stringify(payload).includes("coverage"))).toBe(true);
});

test("restored 40-answer session ends with thanks, explains the cap, and generates an incomplete report by confirmation", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("v6:tts-enabled", "false"));
  const state = await installMockBackend(page, { atTechnicalCap: true });

  await page.goto(`/assessment/session/${UUID}`);
  await expect(page.getByText("已进行 40 轮问答", { exact: true })).toBeVisible();
  await expect(page.getByText(CAP_THANK_YOU, { exact: true })).toBeVisible();
  expect(state.turns.at(-1)?.content).toBe(CAP_THANK_YOU);

  const capCard = page.locator(".technical-limit-card");
  await expect(capCard).toBeVisible();
  await expect(capCard).toContainText("达到 40 次回答的技术保护上限");
  await expect(capCard).toContainText("不代表你的回答不充分、质量不高或能力不足");
  await expect(capCard).toContainText("不会单独决定是否付酬");
  await expect(page.getByLabel("你的回答")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "继续补充" })).toHaveCount(0);

  await page.getByRole("button", { name: "根据已有回答生成报告" }).click();
  const confirmation = page.getByRole("dialog", { name: "根据已有回答生成报告？" });
  await expect(confirmation).toBeVisible();
  await expect(confirmation).toContainText("尚未能从逐字稿中为所有观察角度找到足够、可核验的原话证据");
  await expect(confirmation).toContainText("达到 40 次回答的技术保护上限");
  await expect(confirmation).toContainText("“证据有限”不等于低分");
  await expect(confirmation.getByRole("button", { name: "暂不生成" })).toBeFocused();

  await confirmation.getByRole("button", { name: "仍然生成报告" }).click();
  await expect(page).toHaveURL(new RegExp(`/assessment/report/${UUID}$`));
  expect(state.finalizePayloads).toHaveLength(1);
  expect(state.finalizePayloads[0]).toMatchObject({
    evidence_check_id: 40,
    expected_transcript_fingerprint: transcriptFingerprint(state),
    allow_incomplete: true,
  });

  const evidenceNote = page.locator(".report-evidence-note");
  await expect(evidenceNote).toBeVisible();
  await expect(page.getByText("未充分测得", { exact: true })).toBeVisible();
  await expect(evidenceNote).toContainText("关于“证据有限”");
  await expect(evidenceNote).toContainText("不等于低分、能力不足或回答质量不高");
  await expect(evidenceNote).toContainText("不会单独决定是否付酬");
});

test("mobile keeps the later 20-character requirement visible", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem("v6:tts-enabled", "false"));
  await installMockBackend(page);
  await startInterview(page);

  await page.getByLabel("你的回答").fill("我还在想。");
  await page.getByLabel("你的回答").press("Enter");
  await expect(page.getByText("已进行 1 轮问答", { exact: true })).toBeVisible();

  await page.getByLabel("你的回答").fill("我再想想。");
  const requirement = page.locator("#answer-requirement");
  await expect(requirement).toBeVisible();
  await expect(requirement).toHaveText(/至少 20 字，还差 \d+ 字/);
  await expect(requirement).not.toContainText("4000");
  await expect(page.getByRole("button", { name: /提交回答/ })).toBeDisabled();
});

test("participant can exit without generating a report", async ({ page }) => {
  await installMockBackend(page);
  await startInterview(page);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "退出不生成报告" }).click();
  await expect(page).toHaveURL(/\/assessment$/);
});
