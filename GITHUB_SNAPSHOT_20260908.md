# 思衡 V6 本地源码快照（2026-09-08）

本分支保存 `critical-thinking-assessment-v6` 主目录在 2026-09-08 的本地最新源码。它是独立归档分支，不是已合并的正式发布版，也没有切换生产部署。

## 版本与来源

| 项目 | 核对结果 |
| --- | --- |
| V6 主目录 | `critical-thinking-assessment-v6` |
| GitHub 仓库 | [aBcDd-zm/critical-thinking-assessment-v6](https://github.com/aBcDd-zm/critical-thinking-assessment-v6) |
| 现有默认主线 | `system/v6-natural-interview-demo` |
| 核对时远端主线提交 | `c3c1117abbcc3d7d8b7a5f184b86090cb26f20c6`，2026-08-12，已合入 PR #17 |
| 本地快照原始提交 | `a8e3f6b17bb861085b77f1d8b7715ed8f61aebfb`，2026-08-27，包含 MCP 协议层和 manifest 回填 |
| 本次上传分支 | `system/v6-local-snapshot-20260908` |
| 正式 Release / tag | 核对时仓库没有正式 Release 或版本 tag |

本地原始提交与远端主线存在分叉：本地侧有 2 个 MCP 提交，远端侧有 11 个独立提交。这里保留本地原始历史及未提交源码，不合并或替换远端主线。因此，日期更晚不表示已包含远端全部功能。

远端主线已合入 V6.2.3 候选及发布决策记录，但其代码默认仍为 `v6.2.1 + enforce`；生产实际启用版本要以运行配置和会话开场 trace 为准。本快照也保留自己的 V6.2.1 主线合同。

`critical-thinking-assessment-v6-p1`、`-p2` 和 `-gate0` 是独立研究工作区。本次检查时，`critical-thinking-assessment-v6拷貝` 的 147 个 Git 跟踪文件与 V6 主目录逐文件一致。

## 本次归档内容

- 原工作区 26 个已跟踪文件的最终内容，包括证据归属候选边界与资格限制、访谈重复问题修复及审计、知情同意与界面展示更新。
- MCP 协议层沿用原始提交中已经跟踪的内容。
- 上传准备时额外同步一条已有联调测试断言：匿名导出文件名已由界面改为 `siheng-anonymous-export.zip`，测试对应更新；未修改产品行为。
- 新增本文记录版本来源、分叉关系和本次验证。

原目录的源码、分支和暂存内容保持原样。未跟踪的交付 ZIP、本机 `.env`、数据库、依赖目录和运行缓存不在新增上传范围内。

## 2026-09-08 验证

在独立工作区使用 Python 3.12、`backend/requirements.txt`、`npm ci` 和匹配的 Playwright Chromium 执行：

| 检查 | 结果 |
| --- | --- |
| 后端 `python -m pytest -q` | 498 passed |
| 前端 `npm run test` | 57 passed |
| `npm run typecheck` | 通过 |
| `npm run build` | 通过 |
| 页面 API Mock E2E | 3 passed |
| Vue → FastAPI Mock 联调 E2E | 5 passed，包含管理员复核保存与匿名导出 |
| `git diff --check` | 通过 |
| 源码文件密钥扫描 | 通过；仅对已有 `.env.production.example` 中明确的 `REPLACE_WITH_A_64_HEX_CHARACTER_SECRET` 示例占位符做精确排除 |

原 Python 3.9 环境缺少 MCP 依赖，原 Playwright 缺少匹配浏览器；补齐独立测试环境后完成以上验证。联调首次发现旧导出文件名断言，修正后全部 5 项通过。测试显式使用 Mock 模型、假语音及临时数据库；本次没有执行真实供应商调用或生产部署。
