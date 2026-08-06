# 思衡 V6 本地验收记录

验收范围：`/Users/abcdd/Projects/厚粲杯开发/critical-thinking-assessment-v6`
运行边界：本文件只记录本机 Demo 验收；不替代腾讯云部署、HTTPS、真实模型或共享网关的验收证据，也不使用生产数据。

## 结论写入规则

本文件是证据模板，不预填“通过”。每项只有在写明日期、命令、输出摘要和运行环境后才可标为 `VERIFIED`。`MODEL_GATEWAY_MODE=mock`、浏览器 API Mock 和假语音只能证明本地协议闭环，不能证明真实 DeepSeek、TTS、麦克风、断网或测量自然度。

> 说明：下表中 2026-08-04 的既有记录早于“管理员登录与复核看板”升级；涉及完整套件的记录在本次改动后必须重新执行，不能直接当作本版本验收证据。

## 环境与端口

- 常规本地：后端 `127.0.0.1:8060`、前端 `127.0.0.1:5176`、SQLite `backend/data/v6.db`。
- 真前后端 Playwright：后端 `8061`、前端 `5177`、临时 SQLite；必须显式 Mock，测试结束后两端口都应关闭。
- 来源与隔离：见 [来源快照清单](artifacts/SOURCE_SNAPSHOT_MANIFEST.md)。不得引用 V5/V4 数据库或填写真实凭据。

## 自动化验收

| 项目 | 命令 | 通过标准 | 状态 |
| --- | --- | --- | --- |
| 后端回归 | `cd backend && .venv/bin/python -m pytest -q` | V6 状态、自然访谈、评分、幂等与安全测试通过 | VERIFIED — 2026-08-04：81 passed；含 Argon2id 登录、Cookie 篡改/过期、CSRF、后台接口拦截、看板脱敏，以及本轮最少 20 字与访谈风格标记断言 |
| 新库迁移 | `make migrate`（在空 V6 SQLite） | 只生成 V6 初始结构，不导入旧会话 | VERIFIED — 2026-08-04：初始 revision `20260804_0001` 升级通过，`alembic check` clean |
| 前端单测/类型 | `cd frontend && npm run test && npm run typecheck` | V6 路由、恢复、语音输入与报告合同通过 | VERIFIED — 2026-08-04：9 files / 26 tests；typecheck 通过，涵盖最少 20 字、Enter 提交、Shift+Enter 换行、中文输入法保护和轮次显示 |
| 生产构建 | `cd frontend && npm run build` | 无 TypeScript/Vite 错误 | VERIFIED — 2026-08-06：通过 |
| 页面 API Mock | `cd frontend && npm run test:e2e` | 同意、自然流、结束、报告、语音不自动提交 | VERIFIED — 2026-08-06：2 / 2 通过 |
| 真栈 Mock | `cd frontend && npm run test:e2e:stack` | 使用 `8061/5177` 与临时库；不访问真实供应商 | VERIFIED — 2026-08-06：5 / 5 通过；Vite 5177 与 Uvicorn 8061 均已干净退出 |
| 管理员后台 | `cd frontend && npm run test:e2e:stack` | 未登录拦截、登录进入看板、刷新恢复、优先复核、保存复核、匿名导出、退出后重新拦截及窄屏入口；不调用真实供应商 | VERIFIED — 2026-08-04：真栈 E2E 覆盖通过 |
| 完整本地套件 | `make test` | 上述检查全部通过 | VERIFIED — 2026-08-06：后端 104 passed、前端 30 tests、类型检查与构建通过、页面 Mock E2E 2 / 2、真栈 E2E 5 / 5；8061/5177 已关闭 |
| 生产配置静态安全检查 | 后端生产资产测试（包含在 `make test`） | 不再注入 Basic Auth、`ADMIN_TOKEN` 或前端认证秘密；生产入口仍有后台/登录限流 | VERIFIED — 2026-08-04：生产资产与 shell 语法断言随 81 项后端测试通过 |
| 生产 Compose/镜像构建 | `docker compose -f docker-compose.production.yml config --quiet` 与镜像构建 | Compose 配置及两张生产镜像可在无真实密钥前构建 | NOT VERIFIED — 当前本机未安装 `docker`，未执行构建或容器启动 |
| 启停与健康 | `make start && make health && make stop && make check-stopped` | `8060/5176` 可用后均无监听 | NOT REVALIDATED — 8060/5176 已有本次改动前启动的本地进程；为避免影响现有本地 Demo，本次未停止或覆盖它 |

重点断言：访谈请求中没有题库、目标维度、coverage、阶段命令或固定轮次；首个非空回答可简短提交，后续普通回答需 20 个可见字符，完整的“不知道”类不确定短答例外；`Shift+Enter` 换行、中文输入法选词不误提交；页面只显示“已进行 N 轮问答”而不显示配额；格式失败只修复一次并保留用户 turn；同键重放不重复写入；第 40 次后不再无限访谈；评分缺证据时为 `null`；匿名 ZIP 不含真实议题与自由文本。

## 真实模型人工验收（单独记录）

在本机环境变量提供真实模型配置后，至少完成以下三条路径，并记录 Prompt/模型版本、时间、会话 UUID、人工观察和任何异常；不得把密钥或用户真实敏感内容写入本文件：

1. “留学申请与人生目标”：检查追问是否承接原话，而不是突然补维度或教用户如何回答。
2. 一个真实项目决策：检查多轮话题自然移动、一次一个主要问题、没有 A/B 选项、能力评价或替用户决策。
3. 短答、跳过和提前结束：检查尊重结束、缺维为 `limited|unmeasured`，不强制补问或打低分。

同一初始回答应与 V5 并行体验一次，形成简短自然度对比：首问来源、轮数变化、是否出现强制反事实/最终整合、是否出现题库感、是否过度解释。此对比只用于产品设计，不证明心理测量效度。

## 未验证边界

在真实供应商、真实麦克风、物理断网、独立参与者、成员 A 的构念/BARS 复核和专家效度分析完成前，必须保持“实验性、非标准化、本地 Demo”表述。不得把健康检查、构建、Mock 或一条真实模型演示说成正式上线、收费可用或跨人可比验证。
