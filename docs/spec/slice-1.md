# Spec：切片 1，准入脚本读取接入结论（只报告）

> 2026-11-19。用户原话：「批准最小切片」（针对 autopr-design 的建议："以 agent-relay 为试点，准入脚本只做一件事：读取接入结论，把信任上限设为 L2，用只报告模式运行，不拦截、不合并；顺带建好中心仓库，并确定 agent 身份"）。
> 上游：[README.md](README.md)（设计 r4/r5，整体未批准）；repo-readiness 的 Spec `/Users/vilin/Documents/haigeerlab/repo-readiness/docs/spec/SPEC.md`（用户 11-19 已批准），重点是 §3.3 接入结论、§6 防火墙、§11 接口清单。
> 本切片**不依赖**设计稿整体获批，也不受需求拷问后续结论的影响：改名、AI 介入等级等都不改变这里的行为。

## 1. 目标

在 agent-relay 的每个 PR 上，出现一个名为 `autopr/admission` 的检查结果。它**读取** repo-readiness 签发的接入结论，**算出**这个 PR 的信任上限，并把依据写清楚。它**不拦截、不合并、不打分以外的任何事**。

它对应 repo-readiness 第一阶段的验收线（其 Spec §12 第 4 条）："agent-relay 达到基础档，接入结论被 Auto PR 准入脚本读取，信任上限 L2"。

## 2. 范围

| 做 | 不做（留给后续切片） |
| --- | --- |
| 读取并校验 `.github/autopr/readiness-conclusion.json`（`repo-readiness.conclusion/v1`） | 解析 `autopr-evidence:v1` 证据评论 |
| 核对结论的工具版本是否在允许列表里 | 跨厂商审查、行为验证的判定 |
| 计算有效上限 = min(结论上限, `trust.yml` 配置, 未关闭的降级 issue) | 风险档计算（`risk-tiers.yml`），本切片里所有 PR 一律按"仓库级上限"处理 |
| 输出检查结果、标签、一条摘要评论 | 自动合并、自动降级、度量 |
| 失败时 fail closed，按 L0 报告 | 修改被测仓库的分支保护或必需检查 |

## 3. 输入

| 输入 | 来源 | 缺失时 |
| --- | --- | --- |
| `.github/autopr/readiness-conclusion.json`，**从 base 分支读取**，不用 PR 分支上的那份 | repo-readiness 生成，经人工合并的 PR 提交（T4） | 上限 L0，原因写"没有接入结论" |
| 结论 schema | repo-readiness 发布版里的 `schemas/conclusion.v1.json`，按 tag + 40 位 SHA 引用 | 本切片不运行，配置错误直接报错 |
| 允许的工具版本列表 `allowed-tools.json` | 中心仓库，T4 | 上限 L0 |
| `.github/autopr/trust.yml`（可选） | 被测仓库，T4 | 视作不额外限制 |
| 未关闭的 `autopr:degrade` issue | 被测仓库 | 本切片只读取、不创建；没有就视作不额外限制 |

## 4. 判定

按顺序判定，任一步失败即停，按 L0 报告：

1. 结论文件存在，且能通过 schema 校验。
2. `repo` 等于当前仓库，`default_branch` 等于当前默认分支。
3. 当前时间早于 `valid_until`。
4. `basis` 里的工具版本和 SHA 在 `allowed-tools.json` 里。`attestation` 的校验机制尚未定（repo-readiness 接口清单 #14），本切片先只核对版本和 SHA，并在摘要里标明"发布证明未校验"。
5. 取结论的 `trust_cap`。如果 PR 只改动某一个应用，且结论里有该应用的条目，就取该应用的上限；跨应用时取最低值；改到不属于任何应用的路径时，按仓库级处理。
6. 有效上限 = min(第 5 步结果, `trust.yml`, 降级 issue)。

本切片里，上限只用于报告，不改变任何人的操作。

## 5. 输出

- **检查结果** `autopr/admission`：始终是 `neutral`（只报告）。标题写"信任上限 L2（只报告）"这样的一句话；详情写判定的每一步、结论文件的 sha256，以及结论的签发和过期时间。
- **标签**：`autopr:cap-L0`、`autopr:cap-L2` 这类，每个 PR 只保留一个。
- **摘要评论**：每个 PR 只保留一条，带标记 `<!-- autopr-admission -->`，每次运行时更新。

## 6. 安全

- 触发用 `pull_request`，**不用 `pull_request_target`**。本切片只需要读 base 分支上的文件和 API，不需要更高权限，正好避开 Scorecard 的 Dangerous-Workflow 高危模式（[standard-references.md](standard-references.md) 第 4 节第 7 条）。
- 不 checkout、不执行 PR 分支上的任何代码。
- 权限只开 `contents: read`、`checks: write`、`pull-requests: write`、`issues: read`。
- 所有 action 按 40 位 SHA 固定，不用 `@v7` 这类标签。
- 来自 fork 的 PR：这时 token 只读，检查结果可能写不上；在摘要里注明"fork PR 无法写入"，不算错误。

## 7. 放在哪里

| 产物 | 位置 | 风险档 |
| --- | --- | --- |
| 可复用 workflow `admission.yml` 与判定脚本 | **中心仓库**（名字待定，见 D1） | T4 |
| 判定脚本的单元测试与夹具 | 中心仓库 | T4 |
| `allowed-tools.json` | 中心仓库 | T4 |
| 调用方 workflow（约 10 行，`uses: <中心仓库>/.github/workflows/admission.yml@<40 位 SHA>`） | agent-relay 的 `.github/workflows/autopr-admission.yml` | T4（由 agent-relay-owner 提交，用户合并） |
| `readiness-conclusion.json` | agent-relay 的 `.github/autopr/` | T4（由 repo-readiness 生成，用户合并） |

判定脚本按 repo-readiness 的约定写：Python 标准库、零依赖，在 macOS 的 `/usr/bin/python3`（3.9）和 ubuntu 上都能跑。

## 8. 验收标准

1. 中心仓库的 CI 跑判定脚本的单元测试，至少覆盖这些夹具：
   - 有效结论（基础档 → L2）；
   - 没有结论（L0）；
   - 结论过期（L0）；
   - 工具版本不在允许列表（L0）；
   - schema 不合法（L0）；
   - `repo` 不匹配（L0）；
   - 按应用取上限：单应用、跨应用、根路径三种；
   - `trust.yml` 更低时取更低值。
2. 在 agent-relay 上开一个测试 PR：
   - 结论还没提交时，检查结果显示 L0、原因"没有接入结论"；
   - 结论提交以后，再开一个测试 PR，显示 L2，详情能对上结论文件的 sha256。
3. 检查结果始终是 `neutral`，没有改动任何分支保护或必需检查。
4. workflow 通过 Scorecard 的 Dangerous-Workflow 和 Token-Permissions 检查（本地跑一次 scorecard，或者人工逐条核对）。

## 9. 依赖与分工

| 谁 | 做什么 | 阻塞本切片吗 |
| --- | --- | --- |
| autopr-design（本会话） | 写判定脚本、可复用 workflow、单元测试 | — |
| 用户 | D1–D3 的决定；合并所有 T4 文件 | 是 |
| readiness-tool | 发布结论 schema 和一个带 tag 的发布版；为 agent-relay 生成结论 | 第 2 条验收的后半部分要用到 |
| agent-relay-owner | 提交调用方 workflow；补齐基础档条款（B5、B8 等），这部分由 repo-readiness 给出补丁 | 第 2 条验收要用到 |

## 10. 开工前要用户决定的

| # | 决定 | 建议 |
| --- | --- | --- |
| D1 | **中心仓库的名字和可见性**。改名的讨论还没结束（需求拷问），而中心仓库名一旦建好，各仓库就要按它引用 | 先用一个**与最终品牌无关的中性名字**，例如 `haigeerlab/autopilot-hub`，私有。以后改名时 GitHub 会自动重定向，引用的是 SHA，影响很小。也可以现在就把系统的名字定下来 |
| D2 | **在 GitHub 上新建这个仓库**，这是对外的操作 | 由你明确同意后，我用 `gh repo create` 建，或者你自己建 |
| D3 | agent 身份（设计 Q2） | **本切片不需要**：只报告、不开 PR、不发证据评论。它推迟到下一个切片再定，不阻塞本切片 |
