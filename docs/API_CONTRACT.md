# 思衡 V6 API 合同

基础路径为 `/api/v1`。会话的公开状态仅允许：

`interviewing | finalizing | completed | exited | safety_stopped`

`interviewing` 不包含阶段、coverage、目标维度或测量轮次。恢复快照会带 `user_answer_count`；客户端可将其如实显示为“已进行 N 轮问答”，但不得渲染为阶段、目标进度或必答数量。`technical_turn_cap` 与 `technical_turn_cap_reached` 只表示 40 次稳定性保护上限，不是测量完成条件。六维快照属于私有后台资产，不随会话快照返回。

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
4. 旧版绑定会话可选 `session_closure_suggested` 或 `session_finalizing`；V6.2 正常轮次不以自然停点产生这两种事件
5. `agent_completed` 或 `error`

`heartbeat` 只用于保持 HTTP 连接，客户端不将它渲染为对话。`agent_completed` 含已持久化的 AI turn、更新后的 session 及可选 `speech_url`。随后服务端为该精确逐字稿异步创建一次证据任务；V6.2.1 enforce 依次执行来源归属、服务端资格判定和仅基于 eligible span ID 的六维评分，shadow 只记录对照且保持旧参与者结果。后台调用不延迟或改变已经交付的访谈回复。每个会话按开场 trace 绑定 Prompt 版本。V6.2 收到模型的自然结束意图时确定性保持 `continue`；用户明确要求结束也不能绕过证据快照与页面确认。API 不向访谈官传入轮次、维度、覆盖率或下一题控制字段。

首个 `content` 只需非空；从第二个回答起，普通回答去除空白后的可见字符数必须至少为 20，完整的明确不确定短答例外。客户端在输入框中即时显示状态或剩余字数；普通 `Enter` 提交有效回答，`Shift+Enter` 保留换行，中文输入法选词期间不得误提交。服务端也会再次校验该限制。

同一 `client_turn_id` 与相同载荷必须回放同一已持久化结果；不同载荷返回 `409 idempotency_payload_mismatch`。在第一个流事件之前发现的输入或幂等错误仍使用 HTTP 4xx；流已开始后的模型/JSON 错误使用 HTTP 200 中的终止 `error` 事件。此时用户回答已经保存，客户端应保留同一键并允许刷新恢复，不能伪造一条固定 AI 问题。

## 结束、报告与语音

- `POST /sessions/{uuid}/report-readiness`：非阻塞查询当前精确逐字稿的增量证据任务，返回 `status: checking|ready|insufficient|failed`、`ready: boolean|null`、`check_id`、`transcript_fingerprint`、`cached`，以及仅供逻辑和审计使用的 `minimum_turns_required`、`minimum_turns_met`。它不返回维度、分数、引文、理由或缺失项；前端不展示最低轮数。`ready=true` 必须同时满足至少 8 个已保存非重复用户回答和六维证据全部充分。`retry_failed=true` 只在用户明确重试时重新领取当前失败任务；普通轮询不会形成无限重试。
- `POST /sessions/{uuid}/closure-suggestions/{closure_turn_id}/accept`：仅为绑定旧 Prompt 的既有会话保留的回滚兼容接口；V6.2 前端不渲染其自然停点入口。
- `POST /sessions/{uuid}/finalize`：请求体为 `{ "evidence_check_id": 12, "expected_transcript_fingerprint": "...", "allow_incomplete": false }`。服务端校验检查属于当前会话、逐字稿和评分资产。`ready` 可直接生成完整报告；`insufficient` 只有在 `allow_incomplete=true` 时生成证据有限报告。八轮前主动生成时会话标记 `ended_early=true`，仍复用同一快照且不重新评分。`checking`、`failed`、陈旧 ID 或指纹不一致均拒绝冻结。
- `POST /sessions/{uuid}/exit`：明确退出且不生成报告。
- `GET /sessions/{uuid}/report`：获取唯一的结构化报告；未完成时返回相应状态错误。
- `GET /sessions/{uuid}/report.pdf`：下载服务端生成的报告 PDF。
- `GET /sessions/{uuid}/turns/{turn_index}/speech`：只合成已持久化 AI turn；不接受任意正文。

报告不包含数字置信度、人格判断、职业/留学排序或跨议题比较。每一维只公开 `sufficient`、`limited` 或 `unmeasured` 之一；只有充分且有可核验用户原话时接口才可带原始 1–5 分。参与者网页和 PDF 将该固定等级换算为 20–100 分的百分制呈现，并显示综合总分：它是证据充分维度的等权平均换算，证据不足维度不显示为 0 分也不计入平均；管理端仍以原始五级分复核。

`ready` 只表示六个维度全部满足数字分数、`sufficient=true` 和至少一条可核验证据。V6.2.1 enforce 中，每条证据还必须绑定同一 readiness check 下经服务端验证的 `eligible` 归属 span，并再校验会话、轮次、偏移、原文切片、哈希、逐字稿指纹和资产指纹；外部材料、给 AI 的问题、裸引用和 uncertain span 不能满足数字分证据条件。系统仅在此时主动显示完整报告入口。`insufficient` 不阻止用户主动生成证据有限报告。`checking` 或 `failed` 必须保持会话开放；不得遗漏最后一轮或复用旧快照。冻结后把同一 `result_data` 提升为 `ScoringRun`、`EvidenceItem` 和 `AssessmentReport`，报告生成阶段模型调用次数为零。

## 管理员认证、复核与导出

- `POST /admin/auth/login`：提交 `{ "username", "password" }`；成功后仅设置 8 小时、`HttpOnly` 的会话 Cookie 和可读 CSRF Cookie，生产环境同时启用 `Secure`，两者均为 `SameSite=Strict`。响应只返回管理员公开资料，不返回访问令牌。
- `GET /admin/auth/me`：恢复当前管理员资料；未登录返回 `401 admin_unauthorized`。
- `POST /admin/auth/logout`：清除会话 Cookie；需携带当前会话和 `X-CSRF-Token`。
- 除登录外，所有 `/admin/*` 接口均要求会话 Cookie；`POST`、`PUT`、`PATCH` 等写操作还必须以 `X-CSRF-Token` 回传 CSRF Cookie。认证失败为 401，CSRF 不匹配为 403。
- `GET /admin/dashboard/overview`：返回会话状态聚合、复核待办、链路健康计数和最多 8 条会话摘要；绝不返回逐字稿、证据原话、复核备注、模型原始输出或未校准置信度。

- `GET /admin/sessions`：可按状态、人工复核状态、`manual_review_recommended` 与搜索词筛选。
- `GET /admin/sessions/{uuid}`：完整对话、逐轮时长、模型/Prompt 版本、调用和修复记录、transcript 指纹、评分运行、逐字证据、质量/安全状态、人工复核与专家评分；V6.2.1 另返回仅供管理员使用的 `evidence_attributions`，含前一问、彩色 span 所需偏移、owner/relation/elicitation、服务端资格、校验状态与拒绝原因。`final_scoring_dimension_keys` 只表示已冻结报告实际采用，`snapshot_used_dimension_keys` 表示最新 readiness 快照拟采用；兼容字段 `used_dimension_keys` 始终等于前者。`evidence_items` 另保留 `source_type=user`、`status=sufficient`、`active_for_scoring=true` 的管理端兼容语义。参与者 API 不返回这些归属细节。
- `PUT /admin/sessions/{uuid}/review`：写入人工复核状态、决定与备注。
- `PUT /admin/sessions/{uuid}/expert-scores`：独立保存专家分数。
- `POST /admin/expert-scores:import`：导入 CSV，逐行报告成功或失败。
- `GET /admin/exports/anonymous`：生成匿名 ZIP；默认不带真实议题、自由文本、原话 quote、自由文本理由、精确时间戳和稳定会话标识。

## 错误语义

- `409 session_not_accepting_turns`：状态不是 `interviewing`。
- `409 idempotency_payload_mismatch`：同一键采用不同提交内容。
- 会话快照通过 `technical_turn_cap` 与 `technical_turn_cap_reached` 显式告知前端技术保护上限状态；当前上限为 40 次已保存回答。达到上限不代表证据已充分。
- `409 technical_turn_cap_reached`：已达 40 次技术保护上限，不再接收新回答；同一 `client_turn_id` 的已保存失败提交仍可恢复。
- `409 stale_evidence_snapshot`：检查 ID、逐字稿指纹或评分资产不是当前精确版本。
- `409 evidence_snapshot_processing`：最后一轮仍在后台整理；会话未冻结，可继续轮询同一任务。
- `409 evidence_insufficient`：当前快照不足且请求未明确允许证据有限报告。
- `503 evidence_snapshot_failed`：当前精确快照失败或缺少有效结果；会话未冻结，可明确重试。
- `422 answer_too_short`：从第二个回答起，普通回答少于 20 个可见字符。首个非空回答及完整匹配“不知道／不清楚／不确定／没想好”等明确不确定表达的后续短答例外；请求、同意或模型结构合同无效仍使用其各自的 `422` 语义。
- `500 turn_processing_failed`：一次修复后访谈官仍失败；用户 turn 已保留。
- NDJSON `model_empty_response`：供应商连续两次没有返回有效内容；用户 turn 已保留，可用原 `client_turn_id` 恢复。
- NDJSON `model_connection_interrupted`：连接、读取或协议传输中断；用户 turn 已保留，可用原 `client_turn_id` 恢复。
- `503 scoring_failed`：评分失败，会话保持 `finalizing`，可幂等重试。
- `503 tts_fallback_required`：语音供应商不可用；文本会话不受影响。
