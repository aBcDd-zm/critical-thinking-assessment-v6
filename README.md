# 思衡 V6：完全自然访谈实验版

思衡 V6 是一个默认在本地验收、可经单独授权部署为教师演示站的实验性访谈 Demo。用户只面对一位访谈官“澄澄”；模型自行决定从哪里开始、怎样承接、是否继续以及何时自然收束。六维构念只作为模型内化的观察视角，**不再驱动实时题库、阶段、coverage 或轮次编排**。

> V6 不是标准化心理测验，也不能用于收费、诊断、人格判断、职业/留学排名或跨用户、跨议题比较。成员 A 的构念、BARS 与专家验证完成前，结果只能理解为本次对话的实验性证据整理。

## 本版边界

- 独立目录、分支和 SQLite：不读取、迁移或修改 V3.3、V4、V5 的会话数据。
- 会话只经历 `interviewing → finalizing → completed`；用户可随时 `exit`，高风险内容进入 `safety_stopped`。
- 首问和每一次追问均由 `natural_interviewer_v6.1.1` 生成。服务端不提供候选题库、目标维度、coverage、阶段命令或六维轮询；访谈官仍自主选择问题内容、顺序与表达，以克制的微观陪伴回应和短关键词承接而非机械复述。
- V6.1 的正式访谈协议要求至少保存 40 个有效用户回答，第 45 个有效回答后由服务端确定性收束，并明确记录为 `finish_reason=technical_limit`，而非模型的自然收束。该门禁只控制数据完整性和结束资格，不指定话题、阶段、维度或题目。页面显示“有效回答 x/40”；40–44 个回答之间，访谈官可在证据足够时自然结束，用户也可主动生成报告。
- 首个有效回答只需包含非空的字母或数字；第 2–45 个有效回答至少包含 20 个经 NFKC 归一化后的字母或数字。标点、空白、emoji 与装饰符号不计数。只有经服务端认可的短、完整澄清意图不计入有效回答；客户端自行标记但不符合契约的内容仍按普通回答处理。安全输入和退出请求可绕过长度门禁。
- 访谈冻结后，独立的 `natural_final_scorer_v6.2.2` 按冻结的 `v6.2.0` 完整六维 BARS 合同对逐字稿做一次性取证。证据不足时必须显示“证据有限”或“未充分测得”，不得补问或猜低分。`v6.2.2` 保留 `v6.2.1` 的结构化输出约束，并仅为终评禁用模型 thinking，避免推理过程挤占完整 JSON 的输出预算；不改变访谈官、BARS 锚点或裁决规则。
- 本版仍是探索性、非标准化 Demo；当前是否已有公网验收须以服务器、HTTPS 和真实模型检查为准。经单独授权时，可使用 [腾讯云 Lighthouse 部署资产](deploy/tencent/README.md) 启动隔离的 V6 容器；它不会复用 V3.3、V4、V5 的数据库或覆盖共享网关。

## 文档入口

- [架构与状态边界](docs/ARCHITECTURE.md)
- [HTTP/NDJSON API 合同](docs/API_CONTRACT.md)
- [六维测量与证据合同](docs/MEASUREMENT_CONTRACT_V6.md)
- [V6 Prompt 交接](docs/V6_PROMPT_HANDOFF.md)
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

访谈轮次继续使用 `DEEPSEEK_MAX_TOKENS=3000` 和 `DEEPSEEK_TIMEOUT_SECONDS=30`；冻结后的六维终评使用独立的 `DEEPSEEK_FINAL_SCORER_MAX_TOKENS=8000` 与 `DEEPSEEK_FINAL_SCORER_TIMEOUT_SECONDS=90`。两组配置不得合并调高：前者控制逐轮延迟和冗长度，后者用于避免完整 BARS JSON 被截断，并给一次性终评合理的传输时间；终评的合同修复仍沿用同一组独立预算。

正式研究招募前，前端构建环境还必须显式设置 `VITE_RESEARCH_CONTACT` 与 `VITE_DATA_RETENTION_NOTICE`，内容应与已定稿的招募/知情同意材料一致。未配置时，生产构建会在入口页阻止开始访谈；本地开发默认只标记为受控测试，不得将该默认文案用于正式招募。项目不会虚构研究联系人、保存期限或伦理审批信息。

## 用户体验合同

- 访谈前必须完成版本化知情同意；自由文本会在本地数据库中保存，并可能按同意说明发送给选定模型服务。
- 用户可编辑语音转写；语音只能填充输入框，绝不自动提交。
- 页面不显示阶段、维度、coverage、目标题、六维轮询、综合总分、数字置信度、人格结论或职业建议；只显示正式协议的“有效回答 x/40”和当前回答长度提示，不公开内部路由或评分状态。
- 达到 40 个有效回答后，用户可主动“结束并生成报告”；任何时候都可“退出访谈”，但不足 40 个有效回答的会话标记为未完成且不生成正式完整报告。刷新后可从持久化会话恢复，同一 `client_turn_id` 不得重复写入回答。

## Git 与来源

活动分支为 `system/v6-report-doubao-20260805`。V6 从 V5 的本地工作区快照建立：来源分支 `system/v5-real-issue-demo`、来源提交 `e7ae0246cfa7c9e3cb1e43c8938c740d4cb945ea`。完整的来源边界、排除项与 SHA-256 聚合指纹见 [来源快照清单](artifacts/SOURCE_SNAPSHOT_MANIFEST.md)。
