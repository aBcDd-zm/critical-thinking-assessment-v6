import { expect, test, type Page, type Route } from "@playwright/test";

const UUID = "e2e-v6-session-0001";

type Phase = "interviewing" | "finalizing" | "completed" | "exited" | "safety_stopped";
type MockState = {
  phase: Phase;
  answers: number;
  turns: Array<Record<string, unknown>>;
  payloads: Array<Record<string, unknown>>;
  persistedClientIds: Set<string>;
};

const INITIAL_QUESTION = "你愿意从最近一直在想的一件事开始聊吗？";

function snapshot(state: MockState) {
  return {
    uuid: UUID,
    phase: state.phase,
    participant: { display_name: "小林" },
    user_answer_count: state.answers,
    turns: state.turns,
    report_available: state.phase === "completed",
    manual_review_recommended: false,
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
      status: index === 0 ? "sufficient" : "limited",
      score: index === 0 ? 4 : null,
      reason: index === 0 ? "有精确的用户原话。" : "证据有限。",
      suggestion: "在相似情境中记录依据和改变条件。",
      evidences: index === 0 ? [{ quote: "我想先确认自己真正重视什么", source_type: "user", turn_index: 1 }] : [],
    })),
    experimental_notice: "实验性、非标准化访谈。",
    disclaimer: "不生成综合总分或人格判断。",
  };
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installMockBackend(page: Page) {
  const state: MockState = {
    phase: "interviewing",
    answers: 0,
    payloads: [],
    persistedClientIds: new Set(),
    turns: [{ id: 1, turn_index: 0, role: "assistant", phase: "interviewing", content: INITIAL_QUESTION }],
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
        const boundedClarification = payload.interaction_kind === "clarification"
          && payload.content.replace(/[\s\p{P}\p{C}\p{S}]/gu, "") === "我没理解请换一种问法";
        if (!boundedClarification) state.answers += 1;
        state.turns.push({ id: state.turns.length + 1, turn_index: state.turns.length, role: "user", phase: "interviewing", ...payload });
      }
      const close = state.answers >= 40;
      const assistant = {
        id: state.turns.length + 1,
        turn_index: state.turns.length,
        role: "assistant",
        phase: close ? "finalizing" : "interviewing",
        content: close ? "谢谢你愿意说这些，我们先在这里收束。" : "听起来这对你很重要；你现在最在意的是什么？",
      };
      state.turns.push(assistant);
      if (close) state.phase = "finalizing";
      const events = [
        { event: "user_turn_saved", data: { turn: state.turns.at(-2) } },
        { event: "agent_started", data: {} },
        { event: "agent_delta", delta: assistant.content },
        { event: "agent_completed", data: { turn: assistant, session_action: close ? "finish" : "continue", finish_reason: close ? "natural_closure" : null } },
        ...(close ? [{ event: "session_finalizing", data: { session: snapshot(state) } }] : []),
      ];
      await route.fulfill({
        status: 200,
        contentType: "application/x-ndjson",
        body: events.map((event) => JSON.stringify(event)).join("\n") + "\n",
      });
      return;
    }
    if (path === `/sessions/${UUID}/finalize` && request.method() === "POST") {
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
  await page.getByLabel("用户名").fill("本地流程验收");
  await page.getByLabel(/我已阅读并同意/).check();
  await page.getByRole("button", { name: /开始访谈/ }).click();
  await expect(page).toHaveURL(new RegExp(`/assessment/session/${UUID}`));
  await expect(page.getByText(INITIAL_QUESTION)).toBeVisible();
}

test("consent → natural conversation → model closing → evidence report", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("v6:tts-enabled", "false"));
  const state = await installMockBackend(page);
  await startInterview(page);

  await expect(page.getByText("第 1 / 最多 12 次回答")).toHaveCount(0);
  await expect(page.getByText("问题界定", { exact: true })).toHaveCount(0);
  await expect(page.getByText("有效回答 0/40", { exact: true })).toBeVisible();
  await page.getByLabel("你的回答").fill("我还在想。");
  await expect(page.getByRole("button", { name: /提交回答/ })).toBeEnabled();
  await expect(page.getByText("首次回答简短也可以，接下来会根据你的话继续聊。", { exact: true })).toBeVisible();
  await page.getByLabel("你的回答").press("Enter");
  await expect(page.getByText("听起来这对你很重要；你现在最在意的是什么？")).toBeVisible();
  await expect(page.getByText("有效回答 1/40", { exact: true })).toBeVisible();

  await page.getByLabel("你的回答").fill("还没想好。");
  await expect(page.getByRole("button", { name: /提交回答/ })).toBeDisabled();
  await expect(page.getByText(/可以再补充你这样想的原因/)).toBeVisible();

  await page.getByRole("button", { name: "没理解，请换个问法" }).click();
  await expect(page.getByText("我没理解，请换一种问法。", { exact: true })).toBeVisible();
  await expect(page.getByText("有效回答 1/40", { exact: true })).toBeVisible();

  state.answers = 39;
  await page.getByLabel("你的回答").fill("我也担心自己会后悔，所以想继续听听不同人的看法并再确认条件。");
  await page.getByLabel("你的回答").press("Enter");
  await expect(page).toHaveURL(new RegExp(`/assessment/report/${UUID}$`));
  await expect(page.getByRole("heading", { name: "访谈结果" })).toBeVisible();
  await expect(page.locator(".radar-chart")).toBeVisible();
  await expect(page.getByText("综合总分", { exact: true })).toHaveCount(0);
  expect(state.answers).toBe(40);
  expect(state.payloads).toHaveLength(3);
  expect(state.payloads[1]).toMatchObject({ interaction_kind: "clarification" });
  expect(state.payloads.every((payload) => !JSON.stringify(payload).includes("coverage"))).toBe(true);
});

test("participant can exit without generating a report", async ({ page }) => {
  await installMockBackend(page);
  await startInterview(page);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "退出访谈", exact: true }).click();
  await expect(page).toHaveURL(/\/assessment$/);
});
