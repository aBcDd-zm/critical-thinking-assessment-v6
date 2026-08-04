# 思衡 V6：完全自然访谈实验版

思衡 V6 是一个**仅限本地验收**的实验性访谈 Demo。用户只面对一位访谈官“澄澄”；模型自行决定从哪里开始、怎样承接、是否继续以及何时自然收束。六维构念只作为模型内化的观察视角，**不再驱动实时题库、阶段、coverage 或轮次编排**。

> V6 不是标准化心理测验，也不能用于收费、诊断、人格判断、职业/留学排名或跨用户、跨议题比较。成员 A 的构念、BARS 与专家验证完成前，结果只能理解为本次对话的实验性证据整理。

## 本版边界

- 独立目录、分支和 SQLite：不读取、迁移或修改 V3.3、V4、V5 的会话数据。
- 会话只经历 `interviewing → finalizing → completed`；用户可随时 `exit`，高风险内容进入 `safety_stopped`。
- 首问和每一次追问均由 `natural_interviewer_v6.0.1` 生成。服务端不提供候选题库、目标维度、coverage、阶段命令或测量轮次上下限；在自然收束前，访谈官会先承接并探查一到两层仍关键的不确定性或反例。
- 40 次用户回答只是不可见的技术防失控上限，不是测量设计，也不应显示为进度。
- 访谈冻结后，独立的 `natural_final_scorer_v6.0.0` 对完整逐字稿做一次性六维取证。证据不足时必须显示“证据有限”或“未充分测得”，不得补问或猜低分。
- 本版没有公网地址、发布、推送、合并或部署授权；`deploy/` 仅保留明确的本地实验边界说明，不能作为上线说明。

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

本地用户端为 <http://127.0.0.1:5176/assessment>，复核台为 <http://127.0.0.1:5176/admin>，后端健康检查为 <http://127.0.0.1:8060/api/v1/health>。真实 Vue → FastAPI 联调测试固定使用临时端口 `5177/8061` 与临时 SQLite，且必须显式设为 Mock 模式；它不能读取、打印或调用真实供应商凭据。

首次使用时，将 `backend/.env.example` 复制为只保存在本机的 `backend/.env`，再按实际需要配置模型和语音。默认 `MODEL_GATEWAY_MODE=real`；缺少 DeepSeek 密钥时开场/访谈会明确失败，不会悄悄改用 Mock 或关键词报告。`make start` 默认固定使用 `deepseek-v4-flash`，避免继承其他项目终端中的 `DEEPSEEK_MODEL`；如需仅为 V6 显式覆盖，可设置 `V6_DEEPSEEK_MODEL`。自动化测试必须显式设置 `MODEL_GATEWAY_MODE=mock` 和 `TTS_MODE=fake`。密钥不得写入 Git、文档、日志、截图或聊天记录。Mock 通过仅说明协议、存储和恢复路径可重复；真实 DeepSeek、TTS、麦克风与断网恢复仍须按 [本地验收清单](LOCAL_ACCEPTANCE.md) 单独记录。

## 用户体验合同

- 访谈前必须完成版本化知情同意；自由文本会在本地数据库中保存，并可能按同意说明发送给选定模型服务。
- 用户可编辑语音转写；语音只能填充输入框，绝不自动提交。
- 页面不显示阶段、维度、coverage、目标题、测量轮次、综合总分、数字置信度、人格结论或职业建议。
- 用户可主动“结束并生成报告”或“退出不生成报告”；刷新后可从持久化会话恢复，同一 `client_turn_id` 不得重复写入回答。

## Git 与来源

活动分支为 `system/v6-natural-interview-demo`。V6 从 V5 的本地工作区快照建立：来源分支 `system/v5-real-issue-demo`、来源提交 `e7ae0246cfa7c9e3cb1e43c8938c740d4cb945ea`。完整的来源边界、排除项与 SHA-256 聚合指纹见 [来源快照清单](artifacts/SOURCE_SNAPSHOT_MANIFEST.md)。
