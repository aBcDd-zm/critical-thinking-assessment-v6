# V6.1 冻结与 PR 交接说明（2026-08-05）

## 1. 交付目的与冻结边界

本交接包供队友审查、整合导师后续建议并部署。本分支在当前 V6.1 基线之上记录 V6.1.1 微观表达 Prompt，
不把尚未完成的真实 UAT 表述为已通过，也不将本轮自动化测试等同于心理测量效度验证。

本轮继续冻结以下内容：

- `natural_interviewer_v6.1.1` Prompt 的微观表达约束与自然访谈逻辑；
- 至少 40 次有效回答、45 次用户回答技术硬上限；
- 首个有效回答非空、第二个有效回答起至少 20 个规范化有效字符的门禁；
- 六维评分规则、完整 BARS 合同与报告逻辑；
- 已冻结的真实 UAT 私有审计证据。

V6 仍是探索性、非标准化研究 Demo。不得据此宣称正式测评效度、诊断能力、
跨用户可比性或大规模生产适用性。

### 1.1 本分支 V6.1.1 Prompt 审计记录

- 访谈官 Prompt：`natural_interviewer_v6.1.1` / `v6.1.1`。
- 系统 Prompt SHA-256：`76c59963cdc639cfc7b7a44e30241b126d4814fd7a0ddca55cdafe623a10245a`。
- 本次只增加克制的极短陪伴性回应、短关键词回声和“不抢戏”约束，同时保留原有
  单一开放问题、反二选一、反教学/咨询/人格判断/评分语言及大段逐字复述审计边界。
- `natural_final_scorer_v6.2.2`、六维测量合同、评分规则、40/45 回答协议和豆包 ASR 均未修改。
- 旧版自然度/UAT 结果只作为历史参考；Prompt 改动后不能直接作为 V6.1.1 的真实访谈通过证据。

## 2. AI 自主主持与“适度放权”边界

访谈官自主决定下一问的主题、顺序和表达，不使用固定题库、固定六维轮询、
coverage 阶段命令或预设问题路径。系统保存用户原话和完整逐字稿，并让模型依据
当前对话自然承接、澄清和追问。

“适度放权”不等于取消工程边界。以下约束仍由服务端确定性执行：

- 用户知情同意、退出和安全请求优先；
- 回答有效性、幂等写入、40/45 次回答协议和格式合同不可由模型绕过；
- 40 次有效回答前不能正式结束；40–44 次间允许用户主动结束或模型在证据充分时自然结束；
  第 45 次用户回答后确定性停止；
- 访谈冻结后，独立终评器只依据冻结逐字稿提取六维原话证据，访谈官不能边问边打分；
- 自动结果只作初步结构化分析，后续研究分析、Prompt 调整和比赛/论文解释仍需人工复核。

## 3. V6.1 已实现行为

### 3.1 回答与完成协议

- `MIN_VALID_ANSWERS=40`，`MAX_USER_ANSWERS=45`；前端显示服务端权威的有效回答进度。
- 首个有效回答只要求包含至少 1 个规范化有效字符。
- 从第二个有效回答起，每次至少包含 20 个 Unicode 字母或数字；空白、标点、Emoji、
  控制字符和装饰符号不能凑数。
- 受限的短澄清请求不计入有效回答数；安全与退出请求不受长度门禁阻挡。

### 3.2 终评、报告与可追溯性

- 参与者的 `POST /sessions/{uuid}/finalize` 与管理员的 `POST /admin/sessions/{uuid}/finalize`
  都复用同一幂等终评逻辑；访谈逐字稿冻结后才允许评分。
- 终评失败可在不重生成访谈问题、不重复写入用户回答的前提下安全重试。
- 终评使用冻结的完整六维 1–5 BARS 合同；证据不足返回证据有限/未充分测得，
  不补问、不猜低分，也不输出误导性的综合总分排名。
- 网页报告与 PDF 共用冻结结果；PDF 嵌入 CJK 字体，避免跨设备空白字形。
- 数据库记录实际返回模型、Prompt/合同版本、非内容调用 provenance 和合同哈希，
  便于后续研究复核。

### 3.3 研究告知与语音边界

- 正式构建必须显式配置研究联系方式与数据保存/删除说明；缺失时入口会阻止正式招募。
- 浏览器原生语音转文字入口仍可使用，但豆包语音**输入**模型本轮明确延期；
  不得把现有浏览器能力描述为已接入豆包 ASR。
- 豆包 TTS 属于另一条可选能力；生产 Compose 从服务器私有 `.env.production` 读取
  `TTS_MODE`，可选 `doubao` 或 `disabled`。本交接不把没有真实 Key 的本地配置测试描述为
  豆包服务已验收；豆包 ASR 仍不接入。

## 4. 2026-08-05 零 API 自动化验收

以下是基线版本的历史自动化证据，均未调用真实模型、未读取真实被试数据；不等同于本分支
V6.1.1 的新 Prompt 真实 UAT：

| 检查 | 结果 |
| --- | --- |
| `backend/.venv/bin/python -m pytest -q` | 通过：177 项 |
| `cd frontend && npm test` | 通过：65 项（9 个测试文件） |
| `cd frontend && npm run typecheck` | 通过 |
| `cd frontend && npm run build` | 通过：Vite 生产构建完成 |
| `backend/.venv/bin/python -m pytest -q backend/tests/test_migration_contract.py` | 通过：3 项；覆盖空库升级、既有 `0001` 升级及迁移往返合同 |
| `sh -n deploy/tencent/deploy.sh` | 通过 |
| `git diff --check` | 通过 |

上述结果证明当前代码的协议、存储、迁移、前端合同和构建闭环可重复，
不代表真实 DeepSeek、真实麦克风、公网、PDF 视觉效果或六场完整真实 UAT 已全部通过。

## 5. 真实 UAT 状态与已知问题

- UAT-01 使用不含个人数据的合成材料，在第 7 次有效回答出现一次
  `multiple_primary_questions`，随后按 fail-fast 规则停止。
- 该问题目前只在一个场景出现，故没有为了单例继续修改 Prompt，避免截图定向调参和过拟合。
- UAT-02 与六场完整真实 UAT **有意延期**：等待导师新建议整合后的版本，再依据实际 diff
  决定旧证据能否沿用以及从哪个门禁继续。
- 队友不需要接手 UAT-02 或六场真实 UAT；本 PR 的部署职责不包含宣称这些项目已通过。
- 私有数据库、账本、sealed 输出和 forensic archive 均保留在仓库外，未进入 Git。

若导师后续提交只改 UI 或说明文本，可复核后沿用未受影响的自动化证据；若改动 Prompt、
访谈编排、完成协议、评分或报告合同，则必须冻结新哈希并重新运行相应门禁，不能把本次
UAT-01 当成新版本通过证据。

## 6. 部署、迁移和基础健康检查

### 6.1 部署前备份与环境

在服务器工程根目录执行：

```sh
cp .env.production.example .env.production
chmod 600 .env.production
```

只在服务器私有的 `.env.production` 中设置：

- `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL=deepseek-v4-flash`；
- `DEEPSEEK_MAX_TOKENS=3000`；
- `DEEPSEEK_FINAL_SCORER_MAX_TOKENS=8000`、
  `DEEPSEEK_FINAL_SCORER_TIMEOUT_SECONDS=90`；
- `ADMIN_USERNAME`、Argon2id `ADMIN_PASSWORD_HASH`、至少 32 字符的
  `ADMIN_JWT_SECRET`；
- 与正式招募/知情同意材料一致的 `VITE_RESEARCH_CONTACT` 和
  `VITE_DATA_RETENTION_NOTICE`。

不得把真实值复制到 Git、前端环境、日志、截图或聊天。不要在生产仓库另建
`backend/.env`；生产 Compose 只读取根目录 `.env.production`。

部署前先对现有数据库执行在线备份：

```sh
./deploy/tencent/backup-sqlite.sh
```

### 6.2 构建、迁移和健康检查

```sh
./deploy/tencent/deploy.sh
curl -fsS http://127.0.0.1:18060/healthz
docker compose --env-file .env.production -f docker-compose.production.yml ps
docker compose --env-file .env.production -f docker-compose.production.yml logs --tail=100 backend frontend
```

后端容器启动时自动执行 `alembic upgrade head`；部署脚本等待 Web 健康和数据库迁移健康。
它不会自动修改共享 Caddy、DNS、TLS 或防火墙。Caddy host block 已由发布者人工核对后，
才可单独执行：

```sh
./deploy/tencent/deploy.sh reload-caddy
```

本 PR 要求的部署后最低检查是：容器状态正常、服务器本机 `/healthz` 返回 200、
数据库迁移健康、参与者入口和管理员登录页可访问。它不要求队友执行 UAT-02 或六场真实 UAT。

### 6.3 回退

1. 保留部署前 SQLite `0600` 备份、上一个已验证提交 SHA 和现有 Caddy 配置备份。
2. 应用异常时先保存 V6 容器日志并停止对外招募。
3. 回退到上一个已验证提交并重新运行部署脚本；除非单独完成迁移兼容性审查，
   不要直接降级或覆盖现有数据库。
4. 停止 V6 容器时可以使用下列命令，但**不得添加 `-v`**：

```sh
docker compose --env-file .env.production -f docker-compose.production.yml down
```

5. 回退后重新核验 loopback 健康、数据库迁移状态、参与者入口和管理员登录。

## 7. 明日继续工作的决策点

队友可从本 PR 继续整合导师的新建议并负责部署。拿到新提交后，先做逐文件 diff：

- 仅 UI/文案：复核构建和相关前端测试，未受影响的协议证据可沿用；
- Prompt/访谈编排：冻结新 Prompt 与依赖哈希，重新执行自然度与多主问题门禁；
- 40/45 协议、评分、报告、迁移：重新执行全部自动化和相应真实全流程验证。

在完成上述 diff 分类前，不自动续跑 UAT-02，也不把旧版本的 UAT 结果迁移为新版本结论。
