# 思衡 V6：完全自然访谈实验版

思衡 V6 是一个默认在本地验收、可经单独授权部署为教师演示站的实验性访谈 Demo。用户只面对一位访谈官“澄澄”；访谈模型依据完整逐字稿自然承接，独立后台任务在每轮后更新六维证据快照。六维构念**不驱动实时题库、阶段、coverage 或固定轮次编排**；只有至少 8 个已保存用户回答且六维证据全部充分时，系统才主动显示完整报告入口。

> V6 不是标准化心理测验，也不能用于收费、诊断、人格判断、职业/留学排名或跨用户、跨议题比较。成员 A 的构念、BARS 与专家验证完成前，结果只能理解为本次对话的实验性证据整理。

## 本版边界

- 独立目录、分支和 SQLite：不读取、迁移或修改 V3.3、V4、V5 的会话数据。
- 会话只经历 `interviewing → finalizing → completed`；用户可随时 `exit`，高风险内容进入 `safety_stopped`。
- 首问和每一次追问默认由 `natural_interviewer_v6.2.0` 生成：从真实、具体、需要判断或权衡的事件开始，沿同一事件柔性深入，正常回复 1—2 句话且最多一个主要问题。自然停顿不再构成结束依据；服务端会压制模型自行作出的自然结束，只有独立证据快照六维全部充分时才主动显示结束入口。每个会话绑定开场 trace 的 Prompt 版本；`v6.1.1`、`v6.0.5`、`v6.0.4` 与 `v6.0.3` 原文及回滚路径完整保留。
- 访谈轮次使用关闭思考、512 tokens、25 秒总预算的低延迟配置。每轮后的 `natural_incremental_evidence_v6.2.1` 同样关闭思考，使用 2000 tokens、15 秒总预算并完全后台运行，不阻塞澄澄回复或下一轮输入。增量整理器只接受用户直接表达的行为证据；没有展示机会或没有说明行动/调整时必须保持 `null/IE`。
- 首个回答只要非空即可提交；从第二个回答起，普通回答仍需 20 个可见字符，完整的“不知道／不清楚／没想好”类表达例外。该例外不改变作答计数、证据语义或自然收束规则。
- 8 个已保存用户回答是主动收束的最低保护线，不是固定结束点；八轮后证据不足仍继续。40 次用户回答仍只是不可见的技术防失控上限。两者都不在前端显示为目标进度。
- 每份增量快照都包含完整六维结果、有效用户原话、理由、置信度、优势和改进重点；未充分维度保持 `null/IE`。用户结束时，服务端校验检查 ID 和逐字稿指纹后将同一快照提升为正式评分与报告，报告阶段不再调用模型或重复评分。
- 本版仍是探索性、非标准化 Demo；当前是否已有公网验收须以服务器、HTTPS 和真实模型检查为准。经单独授权时，可使用 [腾讯云 Lighthouse 部署资产](deploy/tencent/README.md) 启动隔离的 V6 容器；它不会复用 V3.3、V4、V5 的数据库或覆盖共享网关。

## 文档入口

- [架构与状态边界](docs/ARCHITECTURE.md)
- [HTTP/NDJSON API 合同](docs/API_CONTRACT.md)
- [六维测量与证据合同](docs/MEASUREMENT_CONTRACT_V6.md)
- [V6 Prompt 交接](docs/V6_PROMPT_HANDOFF.md)
- [V6.2 异步增量取证决策记录](docs/V6_2_INCREMENTAL_EVIDENCE.md)
- [V6.1.1 引导与结束确认候选决策记录](docs/V6_1_1_GUIDED_CLOSURE_DECISION.md)
- [本地验收清单](LOCAL_ACCEPTANCE.md)
- [来源快照清单](artifacts/SOURCE_SNAPSHOT_MANIFEST.md)

## 本地运行

```bash
make setup
make migrate
make test
make start
make health
make stop
make check-stopped
```

本地用户端为 <http://127.0.0.1:5176/assessment>，管理员登录页为 <http://127.0.0.1:5176/admin/login>，后端健康检查为 <http://127.0.0.1:8060/api/v1/health>。真实 Vue → FastAPI 联调测试固定使用临时端口 `5177/8061` 与临时 SQLite，且必须显式设为 Mock 模式；它不能读取、打印或调用真实供应商凭据。

首次使用时，将 `backend/.env.example` 复制为只保存在本机的 `backend/.env`，再按实际需要配置模型和语音。管理员后台也必须设置 `ADMIN_USERNAME`、`ADMIN_PASSWORD_HASH` 和至少 32 位的 `ADMIN_JWT_SECRET`；运行 `make admin-password-hash` 会在可信终端交互式生成 Argon2id 哈希，明文密码不会写入文件。需要重置密码时，在服务器本地再次运行同一命令，并替换环境中的哈希后重启后端。登录态只保存在 8 小时的 HttpOnly Cookie 中（生产环境为 Secure、SameSite=Strict），前端不保存令牌或密码。默认 `MODEL_GATEWAY_MODE=real`；缺少 DeepSeek 密钥时开场/访谈会明确失败，不会悄悄改用 Mock 或关键词报告。`make start` 默认固定使用 `deepseek-v4-flash`，避免继承其他项目终端中的 `DEEPSEEK_MODEL`；如需仅为 V6 显式覆盖，可设置 `V6_DEEPSEEK_MODEL`。自动化测试必须显式设置 `MODEL_GATEWAY_MODE=mock` 和 `TTS_MODE=fake`。密钥不得写入 Git、文档、日志、截图或聊天记录。Mock 通过仅说明协议、存储和恢复路径可重复；真实 DeepSeek、TTS、麦克风与断网恢复仍须按 [本地验收清单](LOCAL_ACCEPTANCE.md) 单独记录。

## 用户体验合同

- 访谈前必须完成版本化知情同意；自由文本会在本地数据库中保存，并可能按同意说明发送给选定模型服务。
- 用户可编辑语音转写；语音只能填充输入框，绝不自动提交。
- 开始页以分层说明明确这不是随意闲聊，要求用户从真实、具体、需要判断或权衡的经历说起；同时准确说明无固定题单或轮数、第二个回答起的 20 个非空白可见字符规则、证据边界与隐私复核范围。
- 页面不显示阶段、维度、coverage、目标题、固定测量轮次、综合总分、数字置信度、人格结论或职业建议；只显示“已进行 N 轮问答”的事实计数，不显示目标或技术上限。
- 用户可随时主动“结束并生成报告”或“退出不生成报告”。至少 8 个已保存回答且六维全部充分后，页面才显示“现有回答已足够生成完整报告”，但不会自动退出；不足时用户仍可确认生成证据有限报告，八轮前生成的报告会标记提前结束。最后一轮快照仍在计算或失败时不得冻结，也不得复用遗漏该轮的旧结果。刷新后可从持久化会话恢复，同一 `client_turn_id` 不得重复写入回答。

## Git 与来源

V6.2 开发分支为 `system/v612-consistent-closure`，基线为 `system/v6-natural-interview-demo`。V6 最初从 V5 的本地工作区快照建立；完整来源边界见 [来源快照清单](artifacts/SOURCE_SNAPSHOT_MANIFEST.md)。
