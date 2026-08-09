# V6.2.1 证据归属与决策主线加固

## 决策

V6.2 的精确子串校验只能证明一段文字出现在某条用户消息中，不能证明该文字由参与者本人形成。V6.2.1 因此把报告准备度改为版本化的两阶段后台链路：

```text
保存逐字稿
  -> 独立证据归属
  -> 服务端资格判定
  -> 仅使用 eligible span 的六维评分
  -> 精确快照
  -> 冻结时原样提升为报告
```

访谈回复不等待这条链路。归属或评分失败时，报告准备度保持失败或处理中；服务端不得回退到未经归属过滤的自动评分。

## 证据归属合同

归属器只描述来源，不做六维评分，也不再自行计算字符偏移。服务端先为每个非空 user turn 生成无缝、非重叠的 `span_candidates`，优先在 #15 显式标记、AI/外部陈述与“但我…”本人理由边界切分。每个候选的 `candidate_id` 绑定候选规则版本、`turn_index/start/end/quote_hash/occurrence`。归属器必须对每个 ID 恰好输出一次，只返回 ID 与 owner、relation、elicitation、来源标签、置信度和简短理由，不回显原文或偏移。服务端按 ID 权威物化原有 span 形状，再按原文切片的 UTF-8 字节计算 SHA-256 并执行严格验证。缺失、重复、未知 ID 或候选身份篡改均失败；相同文字以精确 occurrence 区分，不使用首次 substring 自动纠偏。明确混合且无可靠切分点的整轮候选由服务端强制为 `uncertain/manual_review`。高置信边界若使归属候选总数超过 100，任务显式失败而不静默丢弃边界。

来源字段：

- `participant_owned`：当前 span 是参与者自己的表达；
- `external_quoted`：明确引用 AI、论文、网页、题目、老师、同学或其他外部原文；
- `external_paraphrased`：参与者在转述外部观点，但尚未形成自己的理由；
- `uncertain`：无法可靠拆分或判断来源。

关系字段：

- `own_reasoning`：参与者自己的理由、比较、行动或调整；
- `endorses`、`critiques`、`rejects`：参与者用自己的话说明对外部材料的采纳、批评或拒绝；
- `quotes_only`：仅展示外部内容；
- `asks_or_requests`：向 AI、老师或他人提出的问题或请求。

提示强度只用于审计：`spontaneous | open_probe | focused_probe | strong_scaffold`。V6.2.1 不据此自动加减分；若某维唯一证据来自强提示，系统建议人工复核。

## 服务端资格门

资格由确定性服务端规则计算，模型不能直接返回“可评分”布尔值：

| 情况 | 自动评分资格 |
| --- | --- |
| `participant_owned + own_reasoning` | eligible |
| 参与者自己的批评、拒绝，或另行切出的采纳理由 | eligible 候选，仍须满足对应 Rubric |
| 只有“我同意／我采纳”的裸表态 | context_only；理由必须另切为 `own_reasoning` |
| 裸赞同、外部原文、外部转述、仅引用、问题或请求 | context_only |
| 来源不明、无法拆分或归属任务修复/失败 | manual_review / IE |

新版评分器只能接收服务端验证后的 span registry，并只能按 `attribution_span_id` 引用证据。它不能读取被排除的原始片段，也不能自行新选 quote。维度解释、优势和优先关注点同样必须绑定 eligible span；最终报告中的原文、轮次和偏移由服务端从 span 还原。

## 决策主线

`natural_interviewer_v6.2.1` 继续保持完全自然访谈：不读取维度覆盖、分数、固定阶段、候选题库或必答槽位。新增的内部导航只用于保持事件级连贯和审计：

- 记录原始待决问题的 user quote 锚点；
- 标记当前焦点及其与主线的关系；
- 来源混合时先中性澄清哪些内容来自外部、参与者最终采纳了什么以及为什么；
- 后续优先回到最终选择、关键依据、实际行动、结果或调整，但用户主动换题、拒绝或原事件无法展开时除外。

这些字段不显示给参与者，也不参与六维路由或报告准备度。

## 版本、兼容与发布

- `natural_opening` trace 同时冻结访谈 Prompt 与 `disabled|shadow|enforce` 归属模式；已创建会话禁止中途换版。缺少该绑定字段的历史会话确定性按 `disabled` 读取。
- 已完成报告不重评；旧证据标记为 `legacy_unclassified`，绝不能回填为 `participant_owned`。
- 新资产指纹同时包含归属 Prompt、紧凑 ID 分类 schema（`evidence-attribution-select-v3-id`）、候选规则（`evidence-span-boundaries-v1`）、资格规则、评分 Prompt、评分 schema（`attributed-evidence-span-ref-v1`）、模型和最低轮次规则。
- 代码、开发示例和生产样例已统一为 `v6.2.1 + enforce`：新会话正式评分只接收 eligible span ID，归属失败时禁止自动评分或回退 legacy。`shadow|disabled` 仍保留供已绑定会话和显式回滚，切换不会改写旧开场 trace。

## 验收边界

自动化必须覆盖混合 AI/论文原文、外部转述、给 AI 的问题、裸赞同、主动批评、引用后给出自己的理由、来源不明、重复 substring、错误偏移、非 eligible span 引用、旧指纹、任务失败/重试/乱序以及旧报告恢复。

真实模型验收还需要双人独立标注 owner、relation 和服务端资格结果；提示强度作为独立审计字段记录，六维证据与 BARS 分数由成员 A 按冻结测量规则另行复核。验收报告应包含外部文本误采、本人推理漏采、弃权、专家一致性、逐维偏差、p95 延迟和提前结束情况。完成这些验证前，系统只能说明“本次访谈中展现的推理表现”，不能声称标准化心理测验、稳定能力、跨人排名或正式效度。

双专家模板与 `evidence-attribution-gold-v2` 评估器分别位于 `artifacts/evidence_attribution_gold_template.jsonl` 和 `backend/scripts/evaluate_evidence_attribution_gold.py`。空模板必须返回 `NOT_VERIFIED` 且不计算部分指标；标注完整但外部/本人/uncertain、合同标签、三条等价轴或六维成对分覆盖不足时只能返回 `PARTIAL`。重复文本的错 occurrence 按 exact-span miss 计入，external、uncertain 和全部 non-eligible 的自动入分结果在 `acceptance_gates` 中单独归零验收。真实 DeepSeek 的 AI 支线回放使用一个已配置为非生产、临时数据库的本地 API：

```bash
V6_REAL_UAT_CONFIRM=1 \
V6_API_BASE_URL=http://127.0.0.1:8062/api/v1 \
backend/.venv/bin/python backend/scripts/check_real_deepseek_v621_mainline.py
```

该脚本记录三轮可见问题、来源澄清/回到决策的启发式检查、重复提问、提前结束、总时长和 inclusive p95；输出仍须盲审，启发式通过不等于自然度或效度通过。

2026-08-09 在一次性 SQLite、`shadow` 和真实 `deepseek-v4-flash` 上完成一次 #15 回放：来源澄清和回到原决策两项启发式通过，未提前结束，三轮 inclusive p95 为 3114.741 ms。最新归属快照将 #15 中给 AI 的问题标为 `context_only/asks_or_requests`，论文原文标为 `context_only/external_quoted/quotes_only`，两者都未被 span-ID 评分器引用。完整非敏感审计记录见 `artifacts/real_deepseek_v621_mainline_uat_20260809.json`。该轮回放仍未进行盲审，第三轮 trace 记录了 `repeated_user_wording`，因此不能代替人工自然度判断。

本版本不接入外部查重或 AI 文本检测器，不通过键盘或粘贴行为推断作者。对没有声明、没有上下文线索的隐蔽复制无法可靠识别。
