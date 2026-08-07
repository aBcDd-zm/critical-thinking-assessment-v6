# 思衡 V6：完全自然访谈实验版

思衡 V6 是一个默认在本地验收、可经单独授权部署为教师演示站的实验性访谈 Demo。用户只面对一位访谈官“澄澄”；模型自行决定从哪里开始、怎样承接、是否继续以及何时自然收束。六维构念只作为模型内化的观察视角，**不再驱动实时题库、阶段、coverage 或轮次编排**。

> V6 不是标准化心理测验，也不能用于收费、诊断、人格判断、职业/留学排名或跨用户、跨议题比较。成员 A 的构念、BARS 与专家验证完成前，结果只能理解为本次对话的实验性证据整理。

## 本版边界

- 独立目录、分支和 SQLite：不读取、迁移或修改 V3.3、V4、V5 的会话数据。
- 会话只经历 `interviewing → finalizing → completed`；用户可随时 `exit`，高风险内容进入 `safety_stopped`。
- 首问和每一次追问默认由 `natural_interviewer_v6.0.4` 生成，可通过服务端环境变量显式回滚到完整保留的 `v6.0.3`。服务端不提供候选题库、目标维度、coverage、阶段命令或测量轮次上下限；访谈官根据当前上下文决定是否做一句有来源的自然承接，不强制“复述—理解—追问”结构，并优先处理用户的纠正、澄清请求及尚未回答且会实质性改变理解的问题。
- 首个回答只要非空即可提交；从第二个回答起，普通回答仍需 20 个可见字符，完整的“不知道／不清楚／没想好”类表达例外。该例外不改变作答计数、证据语义或自然收束规则。
- 40 次用户回答只是不可见的技术防失控上限，不是测量设计，也不应显示为进度。
- 访谈冻结后，独立的 `natural_final_scorer_v6.0.0` 对完整逐字稿做一次性六维取证。证据不足时必须显示“证据有限”或“未充分测得”，不得补问或猜低分。
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

## 用户体验合同

- 访谈前必须完成版本化知情同意；自由文本会在本地数据库中保存，并可能按同意说明发送给选定模型服务。
- 用户可编辑语音转写；语音只能填充输入框，绝不自动提交。
- 页面不显示阶段、维度、coverage、目标题、固定测量轮次、综合总分、数字置信度、人格结论或职业建议；只显示“已进行 N 轮问答”的事实计数，不显示目标或技术上限。
- 用户可主动“结束并生成报告”或“退出不生成报告”；刷新后可从持久化会话恢复，同一 `client_turn_id` 不得重复写入回答。

## Git 与来源

活动分支为 `system/v6-natural-interview-demo`。V6 从 V5 的本地工作区快照建立：来源分支 `system/v5-real-issue-demo`、来源提交 `e7ae0246cfa7c9e3cb1e43c8938c740d4cb945ea`。完整的来源边界、排除项与 SHA-256 聚合指纹见 [来源快照清单](artifacts/SOURCE_SNAPSHOT_MANIFEST.md)。
