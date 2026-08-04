# 思衡 V6 本地验收记录

验收范围：`/Users/abcdd/Projects/厚粲杯开发/critical-thinking-assessment-v6`
运行边界：本机 Demo；不部署、不推送、不合并、不使用生产数据。

## 结论写入规则

本文件是证据模板，不预填“通过”。每项只有在写明日期、命令、输出摘要和运行环境后才可标为 `VERIFIED`。`MODEL_GATEWAY_MODE=mock`、浏览器 API Mock 和假语音只能证明本地协议闭环，不能证明真实 DeepSeek、TTS、麦克风、断网或测量自然度。

## 环境与端口

- 常规本地：后端 `127.0.0.1:8060`、前端 `127.0.0.1:5176`、SQLite `backend/data/v6.db`。
- 真前后端 Playwright：后端 `8061`、前端 `5177`、临时 SQLite；必须显式 Mock，测试结束后两端口都应关闭。
- 来源与隔离：见 [来源快照清单](artifacts/SOURCE_SNAPSHOT_MANIFEST.md)。不得引用 V5/V4 数据库或填写真实凭据。

## 自动化验收

| 项目 | 命令 | 通过标准 | 状态 |
| --- | --- | --- | --- |
| 后端回归 | `cd backend && .venv/bin/python -m pytest -q` | V6 状态、自然访谈、评分、幂等与安全测试通过 | VERIFIED — 2026-08-04：68 passed；`compileall` 通过 |
| 新库迁移 | `make migrate`（在空 V6 SQLite） | 只生成 V6 初始结构，不导入旧会话 | VERIFIED — 2026-08-04：初始 revision `20260804_0001` 升级通过，`alembic check` clean |
| 前端单测/类型 | `cd frontend && npm run test && npm run typecheck` | V6 路由、恢复、语音输入与报告合同通过 | VERIFIED — 2026-08-04：9 files / 22 tests；typecheck 通过 |
| 生产构建 | `cd frontend && npm run build` | 无 TypeScript/Vite 错误 | VERIFIED — 2026-08-04：通过 |
| 页面 API Mock | `cd frontend && npm run test:e2e` | 同意、自然流、结束、报告、语音不自动提交 | VERIFIED — 2026-08-04：2 / 2 通过 |
| 真栈 Mock | `cd frontend && npm run test:e2e:stack` | 使用 `8061/5177` 与临时库；不访问真实供应商 | VERIFIED — 2026-08-04：2 / 2 通过；Vite 5177 与 Uvicorn 8061 均已干净退出 |
| 完整本地套件 | `make test` | 上述检查全部通过 | VERIFIED — 2026-08-04：后端 68 passed、前端 22 tests、构建通过、Mock E2E 2 / 2、真栈 E2E 2 / 2；8061/5177 已关闭 |
| 启停与健康 | `make start && make health && make stop && make check-stopped` | `8060/5176` 可用后均无监听 | VERIFIED — 2026-08-04：Alembic 升级 `20260804_0001`，健康检查通过，两个 PID 正常停止，端口均关闭 |

重点断言：访谈请求中没有题库、目标维度、coverage、阶段命令或固定轮次；格式失败只修复一次并保留用户 turn；同键重放不重复写入；第 40 次后不再无限访谈；评分缺证据时为 `null`；匿名 ZIP 不含真实议题与自由文本。

## 真实模型人工验收（单独记录）

在本机环境变量提供真实模型配置后，至少完成以下三条路径，并记录 Prompt/模型版本、时间、会话 UUID、人工观察和任何异常；不得把密钥或用户真实敏感内容写入本文件：

1. “留学申请与人生目标”：检查追问是否承接原话，而不是突然补维度或教用户如何回答。
2. 一个真实项目决策：检查多轮话题自然移动、一次一个主要问题、没有 A/B 选项、能力评价或替用户决策。
3. 短答、跳过和提前结束：检查尊重结束、缺维为 `limited|unmeasured`，不强制补问或打低分。

同一初始回答应与 V5 并行体验一次，形成简短自然度对比：首问来源、轮数变化、是否出现强制反事实/最终整合、是否出现题库感、是否过度解释。此对比只用于产品设计，不证明心理测量效度。

## 未验证边界

在真实供应商、真实麦克风、物理断网、独立参与者、成员 A 的构念/BARS 复核和专家效度分析完成前，必须保持“实验性、非标准化、本地 Demo”表述。不得把健康检查、构建、Mock 或一条真实模型演示说成正式上线、收费可用或跨人可比验证。
