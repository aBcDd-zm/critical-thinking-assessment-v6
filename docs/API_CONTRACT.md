# 思衡 V6 API 合同

基础路径为 `/api/v1`。会话的公开状态仅允许：

`interviewing | finalizing | completed | exited | safety_stopped`

`interviewing` 不包含阶段、coverage、目标维度或测量轮次。恢复快照会带 `user_answer_count`；客户端可将其如实显示为“已进行 N 轮问答”，但不得渲染为阶段、目标进度或必答数量。快照同时带 `technical_turn_cap` 与 `technical_turn_cap_reached`，它们只表示 40 次的稳定性保护上限，不是测量完成条件。快照还带 transcript 指纹以支持审计，但不带实时评分或路由结论。开启用户确认结束合同的快照可另带当前 `closure_suggestion`：`closure_turn_id`、精确 transcript 指纹与 `finish_reason`；它只是可接受或可被后续回答取代的结束建议。

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
3. 等待模型时每 10 秒可出现 `heartbeat`，并有零或多个 `agent_delta`
4. 可选 `session_closure_suggested`（用户确认结束合同下的自然结束意图）或 `session_finalizing`（仅收到有效终局结束时）
5. `agent_completed` 或 `error`

`heartbeat` 只用于保持 HTTP 连接，客户端不将它渲染为对话。`session_finalizing` 提示 UI 停止继续输入并单独调用 `/finalize`；转入 `finalizing` 的本轮不再同步等待最终评分。真正的本轮 session 快照仍以 `agent_completed` 为准。`agent_completed` 含已持久化的 AI turn、更新后的 session 及可选 `speech_url`。模型只允许输出 `interviewer_message`、`session_action` 和 `finish_reason`。每个会话按开场 trace 绑定 Prompt 版本；进程配置变更不会使旧会话中途换版。显式候选 `v6.1.1` 或新版用户确认结束说明下的会话收到 `finish/enough_understanding|natural_closure` 时，服务端记录原始模型意图，把参与者可见文案确定性归一为“可结束且仍可继续补充”，并把对外有效动作映射为 `suggest_finish`，发出 `session_closure_suggested`，保持 `interviewing`。只有旧说明下绑定旧 Prompt 的会话保留历史冻结语义。模型输出 `finish/user_requested` 只用于用户明确结束本次访谈或生成本次报告，并直接进入 `finalizing`；事件或方案“结束”的叙述不得视为该意图。API 不向模型传入轮次、维度或下一题控制字段。

`content` 去除空白后的可见字符数必须至少为 20。客户端在输入框中即时显示剩余字数；普通 `Enter` 提交有效回答，`Shift+Enter` 保留换行，中文输入法选词期间不得误提交。服务端也会再次校验该限制。

同一 `client_turn_id` 与相同载荷必须回放同一已持久化结果；不同载荷返回 `409 idempotency_payload_mismatch`。在第一个流事件之前发现的输入或幂等错误仍使用 HTTP 4xx；流已开始后的模型/JSON 错误使用 HTTP 200 中的终止 `error` 事件。此时用户回答已经保存，客户端应保留同一键并允许刷新恢复，不能伪造一条固定 AI 问题。

## 结束、报告与语音

- `POST /sessions/{uuid}/report-readiness`：在用户主动结束前，对当前未冻结逐字稿执行可选的六维证据准备度预检。预检复用正式终评的证据验证规则，并以会话、精确逐字稿指纹和评分资产指纹作为幂等键。公开响应只含 `status: ready|insufficient|checking`、`ready: boolean|null` 和 `cached: boolean`；不返回缺失维度、分数、引文、理由或逐字稿指纹。预检不冻结会话，不写入正式 `ScoringRun`、`EvidenceItem` 或报告，也不作为生成报告的强制前置条件。
- `POST /sessions/{uuid}/closure-suggestions/{closure_turn_id}/accept`：接受仍为最新状态的结束建议。请求体必须提供 `expected_transcript_fingerprint`；服务端同时校验会话、建议 turn、最新逐字稿指纹与建议未被后续回答取代，成功后才冻结并生成/重试报告。陈旧或不匹配建议返回 `409 stale_closure_suggestion`；相同建议与指纹的重试保持幂等。
- `POST /sessions/{uuid}/finalize`：用户主动结束访谈，或对已经冻结且评分失败的会话作幂等评分重试。它不检查阶段、coverage、题库或固定轮次。
- `POST /sessions/{uuid}/exit`：明确退出且不生成报告。
- `GET /sessions/{uuid}/report`：获取唯一的结构化报告；未完成时返回相应状态错误。
- `GET /sessions/{uuid}/report.pdf`：下载服务端生成的报告 PDF。
- `GET /sessions/{uuid}/turns/{turn_index}/speech`：只合成已持久化 AI turn；不接受任意正文。

报告不包含数字置信度、人格判断、职业/留学排序或跨议题比较。每一维只公开 `sufficient`、`limited` 或 `unmeasured` 之一；只有充分且有可核验用户原话时接口才可带原始 1–5 分。参与者网页和 PDF 将该固定等级换算为 20–100 分的百分制呈现，并显示综合总分：它是证据充分维度的等权平均换算，证据不足维度不显示为 0 分也不计入平均；管理端仍以原始五级分复核。

`ready` 只表示当前逐字稿在现有终评规则下，六个维度当下均有充分且可核验的用户原话证据；`insufficient` 只用于建议继续访谈，不向用户暴露具体维度，也不阻止用户仍按已有回答生成报告。`checking` 或预检失败同样 fail-open。检查期间若逐字稿或会话状态已改变，旧结果不会展示为当前准备度；异常留下的处理中任务也只能在安全租约超时后被重新执行。正式 `finalize` 会冻结当时逐字稿并重新独立评分；不复用预检的临时结果。

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
- 会话快照通过 `technical_turn_cap` 与 `technical_turn_cap_reached` 显式告知前端技术保护上限状态；当前上限为 40 次已保存回答。达到上限不代表证据已充分。
- `409 technical_turn_cap_reached`：已达 40 次技术保护上限，不再接收新回答；同一 `client_turn_id` 的已保存失败提交仍可恢复。
- `422 answer_too_short`：从第二个回答起，普通回答少于 20 个可见字符。首个非空回答及完整匹配“不知道／不清楚／不确定／没想好”等明确不确定表达的后续短答例外；请求、同意或模型结构合同无效仍使用其各自的 `422` 语义。
- `500 turn_processing_failed`：一次修复后访谈官仍失败；用户 turn 已保留。
- `503 scoring_failed`：评分失败，会话保持 `finalizing`，可幂等重试。
- `503 tts_fallback_required`：语音供应商不可用；文本会话不受影响。
