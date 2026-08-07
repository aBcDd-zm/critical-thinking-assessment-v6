import { expect, test, type Page, type Route } from "@playwright/test";

const UUID = "e2e-v6-session-0001";

type Phase = "interviewing" | "finalizing" | "completed" | "exited" | "safety_stopped";
type MockState = {
  phase: Phase;
  answers: number;
  turns: Array<Record<string, unknown>>;
  payloads: Array<Record<string, unknown>>;
  persistedClientIds: Set<string>;
  closureSuggestion: null | {
    closure_turn_id: number;
    transcript_fingerprint: string;
    finish_reason: "natural_closure";
  };
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
    closure_suggestion: state.closureSuggestion,
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
      evidences: index === 0 ? [{ quote: "我想先确认自己真正重视什么", source_type: "user", turn_index: 3 }] : [],
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
    closureSuggestion: null,
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
        state.answers += 1;
        state.turns.push({ id: state.turns.length + 1, turn_index: state.turns.length, role: "user", phase: "interviewing", ...payload });
      }
      const close = state.answers >= 2;
      const assistant = {
        id: state.turns.length + 1,
        turn_index: state.turns.length,
        role: "assistant",
        phase: "interviewing",
        content: close ? "这件事已经梳理得比较完整，可以考虑在这里结束；如果还有重要内容，你仍可以继续补充。" : "听起来这对你很重要；你现在最在意的是什么？",
      };
      state.turns.push(assistant);
      if (close) {
        state.closureSuggestion = {
          closure_turn_id: Number(assistant.id),
          transcript_fingerprint: "a".repeat(64),
          finish_reason: "natural_closure",
        };
      }
      const events = [
        { event: "user_turn_saved", data: { turn: state.turns.at(-2) } },
        { event: "agent_started", data: {} },
        { event: "agent_delta", delta: assistant.content },
        ...(close ? [{
          event: "session_closure_suggested",
          data: { session_uuid: UUID, ...state.closureSuggestion! },
        }] : []),
        { event: "agent_completed", data: { turn: assistant, session_action: close ? "suggest_finish" : "continue", finish_reason: close ? "natural_closure" : null, session: snapshot(state) } },
      ];
      await route.fulfill({
        status: 200,
        contentType: "application/x-ndjson",
        body: events.map((event) => JSON.stringify(event)).join("\n") + "\n",
      });
      return;
    }
    if (path === `/sessions/${UUID}/report-readiness` && request.method() === "POST") {
      await fulfillJson(route, { status: "ready", ready: true, cached: false });
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
  await page.getByLabel("参与编号或昵称").fill("本地流程验收");
  await page.getByLabel(/我已阅读并理解/).check();
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
  await expect(page.getByText("已进行 0 轮问答", { exact: true })).toBeVisible();
  await page.getByLabel("你的回答").fill("我还在想。");
  await expect(page.getByRole("button", { name: /提交回答/ })).toBeEnabled();
  await expect(page.getByText("首次回答可以简短", { exact: false })).toBeVisible();
  await page.getByLabel("你的回答").press("Enter");
  await expect(page.getByText("听起来这对你很重要；你现在最在意的是什么？")).toBeVisible();
  await expect(page.getByText("已进行 1 轮问答", { exact: true })).toBeVisible();

  await page.getByLabel("你的回答").fill("我再想想。");
  await expect(page.getByRole("button", { name: /提交回答/ })).toBeDisabled();
  await expect(page.getByText(/还差 \d+ 字/)).toBeVisible();
  await page.getByLabel("你的回答").fill("我想先确认自己真正重视什么，也想弄清楚这个选择会带来的变化。");
  await page.getByLabel("你的回答").press("Enter");
  await expect(page.getByText("这段对话可以在这里收束", { exact: true })).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "结束并生成报告" }).click();
  await expect(page).toHaveURL(new RegExp(`/assessment/report/${UUID}$`));
  await expect(page.getByRole("heading", { name: "访谈结果" })).toBeVisible();
  await expect(page.locator(".radar-chart")).toBeVisible();
  await expect(page.getByText("综合总分", { exact: true })).toBeVisible();
  expect(state.answers).toBe(2);
  expect(state.payloads).toHaveLength(2);
  expect(state.payloads.every((payload) => !JSON.stringify(payload).includes("coverage"))).toBe(true);
});

test("participant can exit without generating a report", async ({ page }) => {
  await installMockBackend(page);
  await startInterview(page);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "退出不生成报告" }).click();
  await expect(page).toHaveURL(/\/assessment$/);
});
