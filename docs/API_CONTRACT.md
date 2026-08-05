# 思衡 V6 API 合同

基础路径为 `/api/v1`。会话的公开状态仅允许：

`interviewing | finalizing | completed | exited | safety_stopped`

`interviewing` 不包含阶段、coverage、目标维度、候选题目或六维轮询。恢复快照会带服务端权威的 `user_answer_count`（兼容名）与 `valid_answer_count`，两者均指有效用户回答数；同时返回 `minimum_valid_answers=40`、`maximum_user_answers=45`、`remaining_required_answers` 与 `can_finalize`。客户端可显示“有效回答 x/40”，但不得把它解释为访谈阶段、维度覆盖或评分进度。快照还带 transcript 指纹以支持审计，但不带实时评分或路由结论。

## 会话与同意

### `POST /sessions`

创建一个独立 V6 会话并保存版本化知情同意：

```json
{
  "consent_version": "v6-research-pilot-2026-08",
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
  "interaction_kind": "answer|clarification",
  "input_mode": "text|voice|voice_edited",
  "answer_duration_ms": 32000
}
```

响应为 NDJSON，顺序为：

1. `user_turn_saved`
2. `agent_started`
3. 零或多个 `agent_delta`
4. 可选 `session_finalizing`（访谈官的 `finish` 被协议接受，或第 45 个有效回答触发技术上限时）
5. `agent_completed` 或 `error`

`session_finalizing` 只提示 UI 停止继续输入并显示收束状态；真正的最终 session 快照仍以 `agent_completed` 为准。`agent_completed` 含已持久化的 AI turn、更新后的 session 及可选 `speech_url`。访谈官只允许输出 `interviewer_message`、`session_action` 和 `finish_reason`；当 `session_action=finish` 且协议允许结束时，服务端转入 `finalizing`。运行时 `completion_gate` 只向模型提供有效回答数、40/45 边界和 `can_model_finish`，不提供题库、阶段、目标维度或下一题。40 个有效回答前，模型的 `finish` 会被结束门禁拒绝；40–44 个回答之间仍由模型依据完整逐字稿判断是否自然收束；第 45 个有效回答后服务端确定性收束，并在公开事件中标记 `finish_reason=technical_limit`。该理由只能由 `protocol_gate` 产生，不是模型自然收束。

首个 `interaction_kind=answer` 只需含至少 1 个经 NFKC 归一化后的字母或数字；第 2–45 个有效回答至少需要 20 个此类字符。空白、标点、控制字符、emoji 与装饰符号不计入长度。`interaction_kind=clarification` 只是客户端提示；只有服务端认可的、最多 24 个可计字符且整句表达解释/重复/换问法意图的独立澄清才不计入有效回答并可短于 20 字。未命中该契约的内容仍按普通回答处理。安全输入和退出请求同样不受长度门禁阻挡。普通 `Enter` 仅在满足当前门禁时提交，`Shift+Enter` 保留换行，中文输入法选词期间不得误提交。

同一 `client_turn_id` 与相同载荷必须回放同一已持久化结果；不同载荷返回 `409 idempotency_payload_mismatch`。模型/JSON 修复用尽时以 `error` 收束流，但用户回答已经保存；客户端应保留同一键并允许刷新恢复，不能伪造一条固定 AI 问题。

## 结束、报告与语音

- `POST /sessions/{uuid}/finalize`：达到 40 个有效回答后由用户主动结束，或对已经冻结且评分失败的会话作幂等评分重试；不足 40 时返回稳定的 409 门禁错误。
- `POST /admin/sessions/{uuid}/finalize`：管理员保护的同一幂等终评/报告重试入口；要求管理员会话 Cookie 与 CSRF，不能重新开启访谈或追加用户回答。
- `POST /sessions/{uuid}/exit`：任何时候都可明确退出；不足 40 个有效回答时标记 `withdrawn_incomplete`，不生成正式完整报告。
- `GET /sessions/{uuid}/report`：获取唯一的结构化报告；未完成时返回相应状态错误。
- `GET /sessions/{uuid}/report.pdf`：下载服务端生成的报告 PDF。
- `GET /sessions/{uuid}/turns/{turn_index}/speech`：只合成已持久化 AI turn；不接受任意正文。

报告不包含综合总分、数字置信度、人格判断、职业/留学排序或跨议题比较。每一维只公开 `sufficient`、`limited` 或 `unmeasured` 之一；只有充分且有可核验用户原话时接口才可带原始 1–5 序数等级。参与者网页和 PDF 保留该 1–5 序数表达，不换算为未经校准的百分制；证据不足维度不显示为 0 或 1，也不进入任何平均或汇总。低等级只描述本次观察，只有 4–5 级才可标记为优势。

## 管理员认证、复核与导出

- `POST /admin/auth/login`：提交 `{ "username", "password" }`；成功后仅设置 8 小时、`HttpOnly` 的会话 Cookie 和可读 CSRF Cookie，生产环境同时启用 `Secure`，两者均为 `SameSite=Strict`。响应只返回管理员公开资料，不返回访问令牌。
- `GET /admin/auth/me`：恢复当前管理员资料；未登录返回 `401 admin_unauthorized`。
- `POST /admin/auth/logout`：清除会话 Cookie；需携带当前会话和 `X-CSRF-Token`。
- 除登录外，所有 `/admin/*` 接口均要求会话 Cookie；`POST`、`PUT`、`PATCH` 等写操作还必须以 `X-CSRF-Token` 回传 CSRF Cookie。认证失败为 401，CSRF 不匹配为 403。
- `GET /admin/dashboard/overview`：返回会话状态聚合、复核待办、链路健康计数和最多 8 条会话摘要；绝不返回逐字稿、证据原话、复核备注、模型原始输出或未校准置信度。

- `GET /admin/sessions`：可按状态、人工复核状态、`manual_review_recommended` 与搜索词筛选。
- `GET /admin/sessions/{uuid}`：完整对话、逐轮时长、模型/Prompt 版本、调用和修复记录、transcript 指纹、评分运行、逐字证据、质量/安全状态、人工复核与专家评分。
- `POST /admin/sessions/{uuid}/finalize`：对 `finalizing` 且报告尚未生成的冻结会话执行终评或重试；最近一次失败写入评分运行并由详情页展示。
- `PUT /admin/sessions/{uuid}/review`：写入人工复核状态、决定与备注。
- `PUT /admin/sessions/{uuid}/expert-scores`：独立保存专家分数。
- `POST /admin/expert-scores:import`：导入 CSV，逐行报告成功或失败。
- `GET /admin/exports/anonymous`：生成匿名 ZIP；默认不带真实议题、自由文本、原话 quote、自由文本理由、精确时间戳和稳定会话标识。

## 错误语义

- `409 session_not_accepting_turns`：状态不是 `interviewing`。
- `409 idempotency_payload_mismatch`：同一键采用不同提交内容。
- `409 minimum_valid_answers_not_reached`：不足 40 个有效回答，响应同时返回当前数量和剩余数量。
- `409 technical_turn_cap_reached`：已达 45 个有效回答的技术硬上限。
- `422 answer_has_no_visible_characters`：输入不含可计数的字母或数字。
- `422 answer_too_short`：第 2–45 个有效回答不足 20 个可计数字符；澄清、安全与退出输入不走此错误。
- `500 turn_processing_failed`：一次修复后访谈官仍失败；用户 turn 已保留。
- `503 scoring_failed`：评分失败，会话保持 `finalizing`，可幂等重试。
- `503 tts_fallback_required`：语音供应商不可用；文本会话不受影响。
