# 思衡 V6 架构合同

## 核心原则：访谈与归属取证并行，结束只由当前证据快照控制

```mermaid
flowchart LR
    U["用户 / 可编辑文本输入"] --> C["知情同意与会话 API"]
    C --> S["V6 会话编排\n状态、幂等、安全、审计"]
    S --> I["natural_interviewer_v6.2.1\n完整逐字稿 → 自然回应 + 私有主线审计"]
    I --> S
    S --> D[("Session / Turns / Traces\nPrompt and model versions")]
    S -. "回复完成后异步" .-> O["EvidenceAttributionAgent\n精确 user spans"]
    O --> Q{"服务端资格门\neligible / context / review"}
    Q -->|"eligible only"| E["natural_attributed_evidence_v6.2.2\n只返回 span IDs"]
    E --> K[("exact transcript + asset snapshot")]
    K --> G{"至少八轮且六维全部充分?"}
    G -->|是| U["显示完整报告入口"]
    G -->|否| S
    U --> F["用户确认后冻结 transcript + SHA-256"]
    K --> F
    F --> R["同一快照提升为正式评分与报告\n模型调用 0 次"]
    R --> D
    D --> A["管理员登录 / 复核看板 / 匿名导出 / PDF"]
```

- 用户只看到“澄澄”、当前已完成的问答轮数和必要的输入提示，不接触六维、评分或后台审计字段；轮数不表示阶段、配额或上限。
- 访谈期模型读完整逐字稿，只返回用户实际可见的自然回应及 `continue|finish`。它不接收目标维度、缺失维度、候选问题、coverage、阶段命令、每维预算或固定轮次。
- 代码默认、开发示例与生产样例统一为 `NATURAL_INTERVIEWER_PROMPT_VERSION=v6.2.1` 和 `EVIDENCE_ATTRIBUTION_MODE=enforce`；`v6.2.0` 及更早版本仍保留供显式回滚。每个会话按 `natural_opening` trace 绑定 Prompt 和归属模式，配置切换只影响新会话。
- 访谈调用关闭思考模式，使用 512 tokens、首次 30 秒且全链路 80 秒封顶。归属与 span 评分分别使用独立的后台证据配置；它们在访谈回复持久化后启动，不占用回复或输入框等待链路。
- 访谈官不读取证据覆盖，也不决定是否可以结束。V6.2.1 的内部 navigation 只引用真实 user span，记录决策锚点、当前焦点和 `core|branch|return|source_clarification|user_switch`，不携带维度或题库。
- 服务端不相信归属器自报的评分资格：先验证 user role、偏移、精确切片、非重叠与 SHA-256，再按 owner/relation 计算资格。评分器看不到 context-only/uncertain span，只能引用同一检查中的 eligible ID。

## 发布模式与会话合同

- 当前新会话发布合同为 `v6.2.1 + enforce`；生产启动只接受这一组合或“旧 Prompt + disabled”的显式回滚组合，并且两者都必须开启证据观察器。以下 `disabled|shadow` 路径只用于已绑定历史会话、显式回滚或审计对照，不得中途改写其开场合同。
- `disabled`：保持 V6.2.0 的原始增量评分链，供旧合同与快速回滚。
- `shadow`：另存归属 span 和 span-ID 评分对照，但参与者 readiness、报告与旧链完全一致；任何 shadow 失败都必须留下 trace，不能伪装成 enforce 成功。
- `enforce`：只对开场 trace 已同时绑定 `v6.2.1 + enforce` 的新会话启用归属硬门。开场 trace 也冻结 attribution mode，因此 shadow 期间已创建的会话不会在部署切换后中途变成 enforce。归属、格式、指纹或 span 引用任一失败时该快照失败，不调用或回退原始整段评分；旧绑定会话继续旧链。

资产指纹包含归属 Prompt、紧凑 candidate-ID 分类 schema、服务端 span 候选规则、资格规则、span 评分 Prompt、模型配置、Rubric 与最低轮次规则。任务只能写入自己的逐字稿/资产行；陈旧或乱序结果不能覆盖当前快照。

后台 queued/running lease 的排队登记目前是单进程内状态，生产镜像因此锁定一个后端容器和 `--workers 1`。扩展到多 worker 或多副本之前，必须先把队列登记与领取 lease 迁移到数据库或分布式协调层，不能直接水平扩容。

## 状态与事务边界

```mermaid
stateDiagram-v2
    [*] --> interviewing: 同意已持久化；首条 AI 问题已保存
    interviewing --> interviewing: 用户回答与访谈回复保存；异步更新精确快照
    interviewing --> interviewing: 快照不足或处理中；继续回答
    interviewing --> finalizing: 当前快照完成且用户确认结束
    finalizing --> completed: 同一快照提升为评分、证据与唯一报告
    interviewing --> exited: 用户主动退出，不生成报告
    interviewing --> safety_stopped: 高风险安全门命中
```

`finalizing` 是冻结点：保存完整逐字稿的 SHA-256 后，不得再接受普通回答。冻结前必须校验客户端提交的检查 ID、逐字稿指纹和当前资产指纹。快照仍在处理或失败时会话保持 `interviewing`，不得遗漏最后一轮或退回旧快照。

V6.2 将模型意图与结束控制彻底分开：`finish/enough_understanding|natural_closure` 会被确定性改为继续追问，不能生成结束卡；`finish/user_requested` 只记录用户意图并提示使用页面结束入口，也不会绕过快照校验。旧 Prompt 与旧建议接受接口仅为既有会话回滚兼容，V6.2 前端不渲染自然停点入口。

## 访谈期服务端只保留硬边界

服务端可以拦截、记录或拒绝以下情况：未同意、第二个及之后少于 20 个可见字符的普通回答、当前会话不接收回答、相同幂等键载荷不同、高风险内容、空/无效 JSON、模型泄露内部 Prompt/评分、明显有害输出以及 40 次技术上限。首个非空回答及完整的明确不确定短答可通过原有链路。普通风格问题只写入质量标记：服务端不把自然问法替换成题库文本，也不替模型指定下一题。

一次回答的原子顺序为：

1. 校验会话、同意、高风险门与 `(session_id, client_turn_id)`。
2. 保存唯一用户 turn、输入方式、作答时长和调用审计；同键相同载荷重放原结果。
3. 第 40 次回答保存后先保留现有高风险安全门；非安全停止时跳过访谈模型，持久化可幂等重放的确定性致谢。其他回答才将完整逐字稿交给会话绑定版本的访谈官；V6.2.1 在 80 秒总预算内最多允许一次瞬态传输重试和一次结构合同修复，最多三次请求，同类连续失败两次后停止。空响应重试只追加 JSON 输出协议提醒，网络重试保持原载荷。
4. 重试仍失败时，用户 turn 保留、会话仍可用、同一键可恢复；空响应与网络错误分别记录，不生成固定兜底问题。
5. AI 回复持久化后立即返回用户，并为当前精确逐字稿创建一个幂等证据任务；任务完成顺序不会覆盖更新版本。enforce 模式在一次任务中绑定归属 span、readiness check 与评分引用。
6. 已保存用户回答至少为 8，且六维全部满足 `score != null`、`sufficient=true` 和至少一个经服务端验证的 eligible span 时，前端才主动显示完整报告入口。第 8 轮不是固定结束点；用户仍可在此前确认生成带提前结束标记的证据有限报告。
7. `/finalize` 只接受当前完成快照；冻结后直接提升其结果，报告阶段不调用模型。

40 次限制仅用于避免意外无限循环。第 40 次回答后服务端保存系统致谢而不再生成新问题，随后不再接受新回答，只允许用户根据当前精确快照生成报告或退出。它不是可解释的测量阈值，也不用于判断回答充分性、作答质量或能力。

## 审计、隐私与可复核性

- 每轮保存用户/AI 文本、`client_turn_id`、输入方式、作答时长、模型与 Prompt 版本、调用状态、尝试次数、真实总耗时、异常类型与质量标记。
- 每个快照保存逐字稿/资产指纹、完整结构化结果、最后处理的用户轮次、充分维度数、模型与 Prompt、尝试次数、耗时及失败信息。归属 span 另存 owner、relation、elicitation、服务端资格/校验原因和版本；正式报告沿用完全相同的分数、证据和充分状态。
- 管理端可查看完整对话、调用链、评分运行、人工复核和专家评分；生产环境通过单管理员的 Argon2id 凭据、8 小时 HttpOnly 会话 Cookie 和 CSRF 校验保护，前端不持有令牌或认证秘密。
- 匿名导出默认排除真实议题、用户自由文本、原话 quote、自由文本理由、精确时间戳和可稳定关联的标识符。
