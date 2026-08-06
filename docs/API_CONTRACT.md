# 思衡 V6 API 合同

基础路径为 `/api/v1`。会话的公开状态仅允许：

`interviewing | finalizing | completed | exited | safety_stopped`

`interviewing` 不包含阶段、coverage、目标维度或测量轮次。恢复快照会带 `user_answer_count`；客户端可将其如实显示为“已进行 N 轮问答”，但不得渲染为阶段、目标进度、必答数量或技术上限。40 次技术保护上限不在公开快照中，只会在命中时返回专用错误。快照还带 transcript 指纹以支持审计，但不带实时评分或路由结论。

## 会话与同意

### `POST /sessions`

创建一个独立 V6 会话并保存版本化知情同意：

```json
{
  "consent_version": "v6.0",
  "consent_given": true,
  "participant": { "display_name": "可选称呼" }
}
```

服务端只在 `consent_given=true` 时创建会话。成功响应为 `{ "session": ..., "initial_turn": ... }`；`initial_turn` 是由访谈官生成且已持久化的自然开场，不得假定固定问题。

### `GET /sessions/{uuid}`

返回可恢复快照、逐字 turn、当前状态、报告可用性和实验性边界。公开响应不得包含实时维度覆盖、评分器思考、未校准置信度或管理员自由文本；逐 turn 的质量标记仅作为会话恢复/安全提示，前端不得把它解释为能力判断。

## 提交回答

### `POST /sessions/{uuid}/turns:stream`

```json
{
  "content": "用户编辑后的回答",
  "client_turn_id": "客户端生成的 UUID",
  "input_mode": "text|voice|voice_edited",
  "answer_duration_ms": 32000
}
```

响应为 NDJSON，顺序为：

1. `user_turn_saved`
2. `agent_started`
3. 零或多个 `agent_delta`
4. 可选 `session_finalizing`（仅访谈官自然选择 `finish` 时）
5. `agent_completed` 或 `error`

`session_finalizing` 只提示 UI 停止继续输入并显示收束状态；真正的最终 session 快照仍以 `agent_completed` 为准。`agent_completed` 含已持久化的 AI turn、更新后的 session 及可选 `speech_url`。访谈官只允许输出 `interviewer_message`、`session_action` 和 `finish_reason`；当 `session_action=finish` 时，服务端转入 `finalizing`，而非要求客户端补任何特定题目。除用户主动结束外，版本化访谈 Prompt 要求在看似完整的方案后先自然探查一到两层关键不确定性、条件或反例；该行为仍由模型根据逐字稿决定，不由 API 传入轮次或维度控制字段。

`content` 去除空白后的可见字符数必须至少为 20。客户端在输入框中即时显示剩余字数；普通 `Enter` 提交有效回答，`Shift+Enter` 保留换行，中文输入法选词期间不得误提交。服务端也会再次校验该限制。

同一 `client_turn_id` 与相同载荷必须回放同一已持久化结果；不同载荷返回 `409 idempotency_payload_mismatch`。模型/JSON 修复用尽时以 `error` 收束流，但用户回答已经保存；客户端应保留同一键并允许刷新恢复，不能伪造一条固定 AI 问题。

## 结束、报告与语音

- `POST /sessions/{uuid}/finalize`：用户主动结束访谈，或对已经冻结且评分失败的会话作幂等评分重试。它不检查阶段、coverage、题库或固定轮次。
- `POST /sessions/{uuid}/exit`：明确退出且不生成报告。
- `GET /sessions/{uuid}/report`：获取唯一的结构化报告；未完成时返回相应状态错误。
- `GET /sessions/{uuid}/report.pdf`：下载服务端生成的报告 PDF。
- `GET /sessions/{uuid}/turns/{turn_index}/speech`：只合成已持久化 AI turn；不接受任意正文。

报告不包含数字置信度、人格判断、职业/留学排序或跨议题比较。每一维只公开 `sufficient`、`limited` 或 `unmeasured` 之一；只有充分且有可核验用户原话时接口才可带原始 1–5 分。参与者网页和 PDF 将该固定等级换算为 20–100 分的百分制呈现，并显示综合总分：它是证据充分维度的等权平均换算，证据不足维度不显示为 0 分也不计入平均；管理端仍以原始五级分复核。

## 管理员认证、复核与导出

- `POST /admin/auth/login`：提交 `{ "username", "password" }`；成功后仅设置 8 小时、`HttpOnly` 的会话 Cookie 和可读 CSRF Cookie，生产环境同时启用 `Secure`，两者均为 `SameSite=Strict`。响应只返回管理员公开资料，不返回访问令牌。
- `GET /admin/auth/me`：恢复当前管理员资料；未登录返回 `401 admin_unauthorized`。
- `POST /admin/auth/logout`：清除会话 Cookie；需携带当前会话和 `X-CSRF-Token`。
- 除登录外，所有 `/admin/*` 接口均要求会话 Cookie；`POST`、`PUT`、`PATCH` 等写操作还必须以 `X-CSRF-Token` 回传 CSRF Cookie。认证失败为 401，CSRF 不匹配为 403。
- `GET /admin/dashboard/overview`：返回会话状态聚合、复核待办、链路健康计数和最多 8 条会话摘要；绝不返回逐字稿、证据原话、复核备注、模型原始输出或未校准置信度。

- `GET /admin/sessions`：可按状态、人工复核状态、`manual_review_recommended` 与搜索词筛选。
- `GET /admin/sessions/{uuid}`：完整对话、逐轮时长、模型/Prompt 版本、调用和修复记录、transcript 指纹、评分运行、逐字证据、质量/安全状态、人工复核与专家评分。
- `PUT /admin/sessions/{uuid}/review`：写入人工复核状态、决定与备注。
- `PUT /admin/sessions/{uuid}/expert-scores`：独立保存专家分数。
- `POST /admin/expert-scores:import`：导入 CSV，逐行报告成功或失败。
- `GET /admin/exports/anonymous`：生成匿名 ZIP；默认不带真实议题、自由文本、原话 quote、自由文本理由、精确时间戳和稳定会话标识。

## 错误语义

- `409 session_not_accepting_turns`：状态不是 `interviewing`。
- `409 idempotency_payload_mismatch`：同一键采用不同提交内容。
- `409 technical_turn_cap_reached`：已达 40 次不可见技术上限。
- `422 answer_too_short`：从第二个回答起，普通回答少于 20 个可见字符。首个非空回答及完整匹配“不知道／不清楚／不确定／没想好”等明确不确定表达的后续短答例外；请求、同意或模型结构合同无效仍使用其各自的 `422` 语义。
- `500 turn_processing_failed`：一次修复后访谈官仍失败；用户 turn 已保留。
- `503 scoring_failed`：评分失败，会话保持 `finalizing`，可幂等重试。
- `503 tts_fallback_required`：语音供应商不可用；文本会话不受影响。
