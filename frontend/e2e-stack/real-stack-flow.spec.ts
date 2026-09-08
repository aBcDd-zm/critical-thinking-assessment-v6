import { expect, test, type Page } from "@playwright/test";

const API_BASE_URL = "http://127.0.0.1:8061/api/v1";

type StreamResult = {
  status: number;
  events: Array<{ event?: string; data?: { session?: { user_answer_count?: number } } }>;
};

async function submitThroughUi(page: Page, answer: string) {
  await page.getByLabel("你的回答").fill(answer);
  await page.getByLabel("你的回答").press("Enter");
}

test("真实 Vue + FastAPI Mock 模型栈：事件开场、幂等恢复、证据收束与报告", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("v6:tts-enabled", "false"));

  await page.goto("/assessment");
  await page.getByLabel("参与编号或昵称").fill("真栈验收");
  await page.getByLabel(/我已阅读并理解/).check();
  await page.getByRole("button", { name: /开始访谈/ }).click();

  await expect(page).toHaveURL(/\/assessment\/session\/[0-9a-f-]+$/);
  const sessionUuid = page.url().split("/").at(-1);
  expect(sessionUuid).toBeTruthy();
  await expect(page.getByText("访谈官 · 澄澄")).toBeVisible();
  await expect(page.getByText(/最多 12 次回答/)).toHaveCount(0);
  await expect(page.getByText("已进行 0 轮问答", { exact: true })).toBeVisible();
  await expect(page.getByText("问题界定", { exact: true })).toHaveCount(0);

  await submitThroughUi(page, "我需要决定是否申请研究项目，想先核实导师和资金条件。");
  await expect(page.getByText("先把焦点放回这件具体经历：当时你真正需要作出的判断是什么？")).toBeVisible();
  await expect(page.getByText("已进行 1 轮问答", { exact: true })).toBeVisible();

  const replayPayload = {
    content: "我还需要再看看项目的实际安排，也想确认它是否符合我目前的长期计划。",
    client_turn_id: "playwright-v6-recovery-0003",
    input_mode: "text",
    answer_duration_ms: 1234,
  };
  const [first, replay] = await page.evaluate(
    async ({ apiBaseUrl, uuid, payload }): Promise<[StreamResult, StreamResult]> => {
      const send = async (): Promise<StreamResult> => {
        const response = await fetch(`${apiBaseUrl}/sessions/${uuid}/turns:stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
          body: JSON.stringify(payload),
        });
        const events = (await response.text())
          .split("\n")
          .filter(Boolean)
          .map((line) => JSON.parse(line));
        return { status: response.status, events };
      };
      return [await send(), await send()];
    },
    { apiBaseUrl: API_BASE_URL, uuid: sessionUuid!, payload: replayPayload },
  );

  for (const result of [first, replay]) {
    expect(result.status).toBe(200);
    expect(result.events.map((event) => event.event)).toEqual([
      "user_turn_saved",
      "agent_started",
      "agent_delta",
      "agent_completed",
    ]);
  }
  expect(replay.events.at(-1)?.data?.session?.user_answer_count).toBe(2);

  const persisted = await page.evaluate(
    async ({ apiBaseUrl, uuid }) => {
      const response = await fetch(`${apiBaseUrl}/sessions/${uuid}`);
      return response.json();
    },
    { apiBaseUrl: API_BASE_URL, uuid: sessionUuid! },
  );
  expect(persisted.user_answer_count).toBe(2);
  expect(persisted.turns.filter((turn: { client_turn_id?: string }) => turn.client_turn_id === replayPayload.client_turn_id)).toHaveLength(1);

  await page.reload();
  await expect(page.getByText(replayPayload.content, { exact: true })).toBeVisible();

  await submitThroughUi(page, "我已经想清楚，决定先申请，并愿意继续说明自己的准备计划和判断依据。");
  await expect(page.getByText(/最可能让你改变现在的决定/)).toBeVisible();

  await submitThroughUi(
    page,
    "如果导师确认无法提供稳定指导，或两周试用没有得到有效反馈，我会暂停申请，重新比较其他项目。",
  );
  await submitThroughUi(
    page,
    "核心问题是是否值得投入；我会核实来源和数据，因为假设可能有反例；也会听导师和团队的角度，比较方案、风险并权衡决定；如果反馈改变，我会调整。",
  );
  await submitThroughUi(
    page,
    "我还会把时间、资金和指导稳定性分别列出来，避免只凭当前情绪作出选择。",
  );
  await submitThroughUi(
    page,
    "我也想确认最坏情况是否能承受，并把退出条件提前写清楚，再继续核对现实限制。",
  );
  await expect(page.getByText("已进行 7 轮问答", { exact: true })).toBeVisible();
  await expect(page.getByText("现有回答已足够生成完整报告", { exact: true })).toHaveCount(0);
  await submitThroughUi(
    page,
    "我先界定核心问题和问题边界；我会核实数据来源，因为现有假设可能有反例；我会考虑导师和团队的不同角度；我会比较方案，权衡后我决定优先试行两周；新反馈出现时我会调整并重新判断。",
  );
  await expect(page.getByText("现有回答已足够生成完整报告", { exact: true })).toBeVisible();
  await expect(page.getByText("这段对话可以在这里收束", { exact: true })).toHaveCount(0);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "结束并生成报告" }).click();
  await expect(page).toHaveURL(new RegExp(`/assessment/report/${sessionUuid}$`));
  await expect(page.getByRole("heading", { name: "访谈结果" })).toBeVisible();
  await expect(page.locator(".radar-chart")).toBeVisible();
  await expect(page.getByText("综合总分", { exact: true })).toBeVisible();
  expect(await page.locator(".dimension-card.status-sufficient").count()).toBeGreaterThan(0);

  const finalized = await page.evaluate(
    async ({ apiBaseUrl, uuid }) => {
      const response = await fetch(`${apiBaseUrl}/sessions/${uuid}`);
      return { status: response.status, body: await response.json() };
    },
    { apiBaseUrl: API_BASE_URL, uuid: sessionUuid! },
  );
  expect(finalized.status).toBe(200);
  expect(finalized.body.phase).toBe("completed");
  expect(finalized.body.transcript_fingerprint).toMatch(/^[0-9a-f]{64}$/);

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 PDF" }).click();
  const pdf = await download;
  expect(pdf.suggestedFilename()).toContain(sessionUuid!.slice(0, 8));
});

test("真实栈中可以在任意时刻主动结束并生成报告", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("v6:tts-enabled", "false"));
  await page.goto("/assessment");
  await page.getByLabel("参与编号或昵称").fill("结束流程验收");
  await page.getByLabel(/我已阅读并理解/).check();
  await page.getByRole("button", { name: /开始访谈/ }).click();
  await expect(page).toHaveURL(/\/assessment\/session\/[0-9a-f-]+$/);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "结束并生成报告" }).click();
  await expect(page).toHaveURL(/\/assessment\/report\/[0-9a-f-]+$/);
});

test("真实栈管理员登录后可进入复核概览并安全退出", async ({ page }) => {
  await page.goto("/admin/dashboard");
  await expect(page).toHaveURL(/\/admin\/login/);
  await page.getByLabel("管理员账号").fill("playwright-admin");
  await page.getByLabel("密码").fill("playwright-admin-password");
  await page.getByRole("button", { name: "登录后台" }).click();
  await expect(page).toHaveURL(/\/admin\/dashboard$/);
  await expect(page.getByRole("heading", { name: "复核概览" })).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(/\/admin\/dashboard$/);
  await expect(page.getByRole("heading", { name: "复核概览" })).toBeVisible();
  await expect(page.getByText("数据概览", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page).toHaveURL(/\/admin\/login$/);
  await page.goto("/admin/sessions");
  await expect(page).toHaveURL(/\/admin\/login/);
});

test("管理员可从优先复核进入详情、保存复核并导出匿名数据", async ({ page }) => {
  await page.goto("/assessment");
  const sessionUuid = await page.evaluate(async (apiBaseUrl) => {
    const created = await fetch(`${apiBaseUrl}/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        consent_version: "v6.0.0",
        consent_given: true,
        participant: { display_name: "优先复核验收", identity_type: "student" },
      }),
    });
    if (!created.ok) throw new Error(`create failed: ${created.status}`);
    const uuid = (await created.json()).session.uuid as string;
    const turn = await fetch(`${apiBaseUrl}/sessions/${uuid}/turns:stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify({
        content: "这件事让我很犹豫，我还没有想清楚该怎样处理，也愿意保留已有回答。",
        client_turn_id: "playwright-admin-evidence-turn",
        input_mode: "text",
        answer_duration_ms: 1000,
      }),
    });
    if (!turn.ok) throw new Error(`turn failed: ${turn.status}`);
    await turn.text();
    const readinessResponse = await fetch(`${apiBaseUrl}/sessions/${uuid}/report-readiness`, { method: "POST" });
    if (!readinessResponse.ok) throw new Error(`readiness failed: ${readinessResponse.status}`);
    const readiness = await readinessResponse.json();
    const finalized = await fetch(`${apiBaseUrl}/sessions/${uuid}/finalize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        evidence_check_id: readiness.check_id,
        expected_transcript_fingerprint: readiness.transcript_fingerprint,
        allow_incomplete: readiness.status !== "ready",
      }),
    });
    if (!finalized.ok) throw new Error(`finalize failed: ${finalized.status}`);
    return uuid;
  }, API_BASE_URL);

  await page.goto("/admin/login");
  await page.getByLabel("管理员账号").fill("playwright-admin");
  await page.getByLabel("密码").fill("playwright-admin-password");
  await page.getByRole("button", { name: "登录后台" }).click();
  await expect(page.getByRole("link", { name: /优先复核/ })).toContainText("优先复核");
  await page.getByRole("link", { name: /优先复核/ }).click();
  await expect(page).toHaveURL(/\/admin\/sessions\?manual_review_recommended=true$/);

  const sessionRow = page.locator("tr").filter({ hasText: "优先复核验收" });
  await expect(sessionRow).toContainText(sessionUuid.slice(0, 8));
  await sessionRow.getByRole("link", { name: /打开复核/ }).click();
  await expect(page).toHaveURL(new RegExp(`/admin/sessions/${sessionUuid}$`));
  await page.getByRole("button", { name: "人工复核" }).click();
  await expect(page.getByLabel("复核人")).toHaveValue("playwright-admin");
  await page.getByLabel("复核状态").selectOption("in_review");
  await page.getByLabel("复核备注").fill("端到端复核保存验证");
  await page.getByRole("button", { name: "保存复核" }).click();
  await expect(page.getByText("人工复核状态已保存。")).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL(/\/admin\/sessions\?manual_review_recommended=true$/);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "匿名导出" }).click();
  expect((await download).suggestedFilename()).toBe("siheng-anonymous-export.zip");

  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page).toHaveURL(/\/admin\/login$/);
  await page.goto(`/admin/sessions/${sessionUuid}`);
  await expect(page).toHaveURL(/\/admin\/login/);
});

test("窄屏后台仍保留用户端与退出入口", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin/login");
  await page.getByLabel("管理员账号").fill("playwright-admin");
  await page.getByLabel("密码").fill("playwright-admin-password");
  await page.getByRole("button", { name: "登录后台" }).click();
  await expect(page.getByRole("link", { name: /打开用户端/ })).toBeVisible();
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(page).toHaveURL(/\/admin\/login$/);
});
