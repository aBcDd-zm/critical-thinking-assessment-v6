# V6.2 异步增量取证与证据驱动结束

> 本文件记录 V6.2.0 的结束与快照基线。V6.2.1 在不改变参与者 API 和“同快照零模型调用提升”语义的前提下，增加独立归属层与 span-ID 评分；当前合同见 [V6.2.1 证据归属与决策主线](V6_2_1_EVIDENCE_ATTRIBUTION.md)。

## 决策

V6.2 取消“访谈官认为自然完整就建议结束”作为当前产品的结束依据。每次用户回答和澄澄回复保存后，服务端为该精确逐字稿异步更新一份完整六维证据快照；访谈回复与下一轮输入不等待该任务。

V6.2.1 增加最低八轮保护。系统仅在已保存的非重复用户回答至少为 8，并且六维全部满足 `score != null`、`sufficient=true` 且至少存在一条经校验的用户原话时，显示：

> 现有回答已足够生成完整报告

系统不会自动退出。用户可以继续回答，也可以在证据不足时主动确认生成证据有限报告。
第 8 轮不是固定结束点；八轮后任一维仍为 `null/IE` 就继续访谈。八轮前主动生成的报告复用当前精确快照并标记 `ended_early=true`，不重新评分。

## 一致性边界

- 一个会话、精确逐字稿指纹和评分资产指纹最多对应一个证据任务。
- 输入是上一份已验证快照和此后新增用户回答；输出始终是完整 `FinalScorerOutput`，不得把每轮分数相加。
- V6.2.0 的 quote 必须指向 user turn，轮次准确，并且是原文连续子串；V6.2.1 enforce 还要求 quote 对应同一检查、同一逐字稿和同一资产下的 eligible attribution span。仅出现在 user turn 不再足以证明观点属于参与者。
- 新逐字稿会立即使前端旧 `ready` 状态失效。旧任务乱序完成只写自己的指纹行，不能覆盖新版本。
- 当前任务处理中或失败时，`/finalize` 不冻结逐字稿；失败只可通过显式重试重新领取。
- 冻结前同时校验检查 ID、逐字稿指纹和评分资产指纹。冻结后直接把该快照提升为正式评分、证据与报告，模型调用次数为零。

## 模型配置

| 调用 | thinking | max tokens | 首次预算 | 总预算 |
| --- | --- | ---: | ---: | ---: |
| `natural_interviewer_v6.2.0` | disabled | 512 | 15 秒 | 25 秒 |
| `natural_incremental_evidence_v6.2.1` | disabled | 2000 | 8 秒 | 15 秒 |
| `natural_interviewer_v6.2.1` | disabled | 512 | 30 秒 | 80 秒 |
| `natural_evidence_attribution_v6.2.1` | disabled | 2000 | 8 秒 | 15 秒 |
| `natural_attributed_evidence_v6.2.2` | disabled | 2000 | 8 秒 | 15 秒 |
| `natural_final_scorer_v6.1.0`（仅旧冻结会话兼容） | enabled | 12000 | 90 秒 | 原兼容路径 |

增量任务最多重试一次，不产生本地替代分数或报告。任务错误、尝试次数和真实耗时会写入检查与 trace。

## API 状态

`POST /sessions/{uuid}/report-readiness` 非阻塞返回：

```json
{
  "status": "checking|ready|insufficient|failed",
  "ready": null,
  "cached": false,
  "check_id": 12,
  "transcript_fingerprint": "64 位 SHA-256",
  "minimum_turns_required": 8,
  "minimum_turns_met": false
}
```

`POST /sessions/{uuid}/finalize` 请求体：

```json
{
  "evidence_check_id": 12,
  "expected_transcript_fingerprint": "64 位 SHA-256",
  "allow_incomplete": false
}
```

`ready` 是“最低八轮已满足且六维证据全部充分”的组合判断，可以生成完整报告；`insufficient` 只有在 `allow_incomplete=true` 时生成证据有限报告；`checking`、`failed` 或指纹不一致不会冻结会话。

## 回滚

`v6.2.0`、`v6.1.1`、`v6.0.5`、`v6.0.4` 和 `v6.0.3` 的 Prompt 原文与哈希保持不变。会话使用 `natural_opening` trace 绑定开场版本，因此环境变量回滚只影响新会话。V6.2.1 新增归属表并扩展证据行，不修改 Rubric、参与者报告公开结构或 API 路径；旧行明确保持 `legacy_unclassified`。

## 验收边界

自动化可验证幂等任务、异步调度、六维门槛、引用校验、失败不冻结、陈旧快照拒绝、同快照提升和报告阶段零模型调用。真实 DeepSeek 的十轮 P95、最终 3 秒打开报告以及用户自然度仍需在部署后的独立真实验收中记录；本文件不把 Mock 结果描述成生产性能证据。
