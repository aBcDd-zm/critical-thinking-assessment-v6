# 证据归属双专家金标评估模板

`evidence_attribution_gold_template.jsonl` 是待标注种子，不是已完成金标集。模板内所有 `expert_a`、`expert_b`、`adjudicated` 和 `system` 标签均为 `null`；任何人不得根据样例语义自动补写，也不得把候选边界当成专家结论。评估合同版本为 `evidence-attribution-gold-v2`。

## 冻结输入和标注

1. 将模板复制为一次独立评估文件，冻结 `case_id`、`span_id`、`text`、`full_answer`、`turn_index`、`start` 和 `end`。每行必须满足 `full_answer[start:end] == text`；同一 case/turn 的金标 span 不得重叠。
2. 给两位专家分别发不含另一位结果的副本。专家 A/B 独立填写自己的三个字段；完成前不得交换判断。
3. 汇总后由约定的裁决人填写 `adjudicated`。裁决标签可以与任一专家不同，但必须有真实裁决记录，不能由脚本推断。
4. 把同一冻结输入送入待测系统，并把与该金标行对齐的系统预测填入 `system`：
   - 有预测时：`abstain=false`，填写三个标签、`turn_index/start/end/text`，并用 `used_for_scoring` 记录该预测是否真的进入数字评分。系统文本必须与其声明的坐标切片一致。
   - 明确弃权时：`abstain=true`，三个标签及 `turn_index/start/end/text` 全为 `null`，`used_for_scoring=false`。
   - 待运行模板可保留 `abstain=null` 并暂不增加新的系统坐标字段；它只能得到 `NOT_VERIFIED`。
5. 如有六维 case 级评分，在 `expert_scores` 和 `system_scores` 中按以下键填 1–5 分：`problem_definition`、`evidence_evaluation`、`reasoning_argumentation`、`multiple_perspectives`、`integrative_decision`、`dynamic_adjustment`。同一 case 的多条 span 若重复携带评分，值必须完全一致。缺少任一维成对分时可计算其他指标，但顶层状态只能是 `PARTIAL`。

## 标签与服务端资格矩阵

标签合法值：

- `owner`：`participant_owned | external_quoted | external_paraphrased | uncertain`
- `relation`：`own_reasoning | endorses | critiques | rejects | quotes_only | asks_or_requests`
- `eligibility`：`eligible | context_only | manual_review`

专家 A/B 和裁决标签必须与服务端硬门一致：

- `owner=uncertain` 必须为 `manual_review`。
- `owner=external_quoted | external_paraphrased` 必须为 `context_only`。
- `owner=participant_owned` 且 `relation=own_reasoning | critiques | rejects` 时必须为 `eligible`。
- `owner=participant_owned` 且 `relation=endorses | quotes_only | asks_or_requests` 时必须为 `context_only`。单纯赞同不能代替参与者自己的采纳理由。

脚本会拒绝违反矩阵的专家或裁决数据。系统预测允许违反矩阵，因为这正是需要计入评估的错误。

## #15 和内容等价样本

模板中的 #15 使用用户提供的精确原文并预切为三个待标边界：第一段截止“这一点让我很难受”，第二段从“这是一次聊天时”到“感觉类似于这种访谈法会好一些”，第三段从“上文利用多种数据来源”到结尾。协调人可以候选路由这三段，但路由词不能写入专家金标，也不能替代两位专家独立判断。

模板另冻结了三组内容等价样本：`length` 比较长/短表达，`register` 比较口语/书面表达，`ai_styling` 比较日常措辞/AI 化格式措辞。每组至少两个唯一 `style_variant`，且组内的 `adjudicated owner/relation/eligibility` 必须一致，否则不能归因为措辞敏感性。`equivalence_group` 不表示作者身份，也不能被归属器作为输入特征。

## 状态、覆盖门和验收门

- `NOT_VERIFIED`：专家、裁决或系统标注未完成。所有指标为 `null`，不输出部分结论。
- `PARTIAL`：标注完整且指标已计算，但覆盖门未全部通过。
- `VERIFIED`：指标可复核，且以下覆盖全部存在：外部 span、本人 eligible 推理、uncertain span、全部合同标签、长短/口语书面/AI 化三轴、六维成对评分，且三个专家标签的 Cohen's κ 都可定义。

`acceptance_gates.coverage` 记录上述覆盖；`acceptance_gates.safety` 要求 external、uncertain 及所有金标 non-eligible span 的 `used_for_scoring` 误入数都为 0。顶层 `VERIFIED` 只表示指标覆盖足够，不会把失败的模型结果伪装成通过；最终是否通过读取 `acceptance_gates.overall_passed`。F1 和 MAE 的质量阈值仍需成员 A 冻结，本脚本不自行发明。

## 指标定义

- 专家一致性：分别对 `owner`、`relation`、`eligibility` 计算一致率和 Cohen's κ，并给出三字段联合完全一致率。任一字段无标签方差时，宏 κ 状态为 `PARTIAL_UNDEFINED_NO_VARIANCE`。
- 外部文本误采率：金标 `owner` 为 `external_quoted | external_paraphrased` 的 span 中，`system.used_for_scoring=true` 的比例。
- uncertain 及全部 non-eligible 误入分率：分别以 `owner=uncertain` 和 `adjudicated.eligibility != eligible` 为分母，统计 `used_for_scoring=true`。
- 本人推理漏采率：金标 `eligibility=eligible` 的 span 中，系统弃权、非 exact span 或未判为 `eligible` 的比例。重复文本的错 occurrence 必须计为漏采。
- Fixed-label Macro F1：对三个合同的完整合法标签集分别计算 Macro F1，再取三任务均值。弃权和非 exact span 均按漏判处理；同时报告每类 gold/predicted support 和缺失标签。
- 弃权率：`system.abstain=true` 的 span 占全部 span 的比例。
- 逐维 MAE：以 case 为计量单位，对成对存在的专家裁决分和系统分计算平均绝对误差。一个 case 的多条 span 只计一次；单侧缺分该维为 `NOT_VERIFIED_UNPAIRED_SCORES`，两侧均无分为 `NOT_AVAILABLE`。
- 内容等价差异：在每个 `equivalence_group` 内两两比较，先得到组内率，再对组等权宏平均，避免多 variant 组获得更大权重。分别报告 `owner`、`relation`、`eligibility`、弃权及 exact-span-match 差异率；三个标签率只使用双方都未弃权的配对，并报告独立分母。

这些指标只评价当前冻结样本上的来源归属、span 恢复和分数偏差，不证明群体公平、标准化效度、稳定能力或跨人可比性。

## 运行与退出码

在 `backend/` 下运行：

```bash
.venv/bin/python scripts/evaluate_evidence_attribution_gold.py \
  ../artifacts/evidence_attribution_gold_template.jsonl
```

- 结构缺字段、坐标/切片不一致、span 重叠、非法标签/资格矩阵、等价组裁决不一致或冲突的重复 case 分数：`INVALID`，退出码 1。
- 未标注或标注不完整：`NOT_VERIFIED`，退出码 2。
- 标注完整但覆盖不足：`PARTIAL`，退出码 2。
- 指标覆盖完整：`VERIFIED`，退出码 0。仍须检查 `acceptance_gates.overall_passed`，不能用退出码替代模型验收结论。

可用 `--output result.json` 同时落盘完整 JSON 结果。
