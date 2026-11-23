# Spec：切片 18，合规监测（autopilot audit）

> 2026-11-23，autopr-design。总账 `research/2026-11-18-自动PR系统设计/audit-spec.md`（用户已批准，原话"批准"；M1–M6 原话"按这样定"）。本文件随切片逐步补全。

## 18a：基线与配置检查

- **基线** `audit/baselines.json`（`autopilot.audit-baselines/v1`）：每个仓库一项，包括 `since`、`approval`（批准原话出处）、`hub_pin`、`takeover {project, modules}`、`checkup {Cn: ok|missing|unknown}`。校验时把所有问题一次列出。这个文件已加进共用的 T4 规则。
- **接管档位**（M3①）：在仓库默认分支的克隆里，按 spec-guard 0.92 的规则读出项目和各模块的档位：
  - 有能力图的仓库读 `.agent/config.json`，`moduleTakeover` 里只要有一项不合法，整个键作废；
  - 其他仓库读 `takeover.yml` 的 `merge`、`release`。

  然后逐项和基线比，比基线宽的一律算"严重"。顺序：spec、plan 为 human < codex；merge 为 human < auto；release 为 human < prepare < auto-confirm < auto。读不懂的也算"严重"。
- **钉**（M3②）：两个调用文件里所有 `uses:@` 和 `hub_ref`，都要等于基线的 `hub_pin`；文件缺失，或者没有钉 autopilot-hub，都算"提醒"。
- **体检**（M3③④）：基线里是 ok、现在不是 ok 的，算"提醒"；基线里没登记的检查项不比。
- **降级**（M3⑤）：本仓库有未关闭的降级 issue，算"提醒"。

基线在 18c 登记了 agent-relay（#194 合并后的样子）和沙盒。

## 18b：Plan 审批检查

只读 spec-guard 0.92 的 `map_view --json`（M1：不重新判定），只看**已开工**的模块。"已开工"指模块已完成，或者 todo 里已有勾选项；spec-guard 的 `step` 只反映 Spec、Plan 文件在不在，不能当作开工的依据。

| 检查 | 判定 |
| --- | --- |
| Plan 没批就开工（M2①） | `review.plan` 不是 approved／codex-approved，或者 `reviewApprovedBy.plan` 为空 → 严重；`revised`（批后又改过）→ 提醒；`none`（项目不用批准标记）→ 判不了，所有这类模块合成一条说明（个数加前 8 个模块名） |
| Codex 批了不该它批的（M2②） | `review.plan` 是 codex-approved，但 `reviewApprovedBy.plan` 为空（spec-guard 认为档位不允许，这次批准不算）→ 严重，并写明 `codexRoute.plan` |
| 重要需求没让你批（M2③） | `important.reasons` 不为空，并且 `review.spec` 是 codex-approved，或者是 pending／invalid → 严重。`revised` 表示你批过、后来改过文件，不算违规 |
| 两轮仍有阻断（M2④） | map_view 没有轮次数据，依靠 spec-guard 自己兜底，说明里写"未独立核验" |
| map_view 读不到 | 提醒，Plan 审批判不了 |

在 module-crew（42 个模块）和沙盒的真实数据上试过：第三条最初的写法把 6 个"你批过、后来改过"的模块误报成违规，改成上表的判定后，两个仓库都没有违规。

## 18c：命令、周报与违规 issue

```
python3 -B audit/cli.py [--repo owner/name ...] [--ref 分支] [--no-post] [--json]
```

- 不写 `--repo` 就查基线里登记的全部仓库；写了但没登记的报"未登记"。`--ref` 查指定分支，供演练用。
- 每个仓库用只读机器人令牌一次性克隆默认分支，在克隆里读档位和钉、跑 hub 体检（不跑检查命令）；有能力图的再跑 map_view。降级 issue 走 API。克隆、体检或接口失败时报"判不了"，不猜。
- **周报**：hub 里一个标签为 `autopilot:audit-report` 的"autopilot 合规周报" issue，只开一次，每次运行追加一条机器人评论。严重项置顶。
- **违规 issue**：同一个仓库的同一类违规（标签 `autopilot:audit`）只开一个 issue；之后再发现就追加"仍存在"，消失了就追加一次"未再发现"。**从不自动关闭**，由人关（M6）。
- **信任**：周报和违规 issue 都要是机器人开的、正文没被改过才认。REST 的 `updated_at` 遇到评论也会变，所以正文有没有改过看 GraphQL 的 `lastEditedAt`，和发布跟踪 issue 的做法一致。
- **退出码**：0 表示无违规；1 表示有违规；2 表示环境或参数有问题，或者有仓库判不了。违规优先报 1。
- `--no-post` 只打印，不在 hub 写任何东西。

**沙盒演练（2026-11-23）**：在沙盒推了一个不合并的 `audit-drill` 分支，造了三处违规：drill-div 的 Plan 标记改回 pending、接管档位从 plan 放宽到 merge、降级调用文件的钉换成别的 hub 提交。报告把它们全部点名，开出 hub#47（钉，提醒）、#48（Plan 未批，严重）、#49（档位放宽，严重）。再跑一次只追加"仍存在"；改查 main（违规已撤掉）后，三个 issue 各追加一次"未再发现"，第二次不再追加，也都没有被关闭。演练分支事后已删除。

演练中发现：评论会改变 issue 的 `updated_at`，旧逻辑因此把自己开的周报当成被改过，又另开了一个。已改成按 `lastEditedAt` 判断，并补了测试。多开出的 #50–#52 已关闭，周报以 #46 为准。

**agent-relay**：只读跑一次，结果合规。9 个带批准标记的已开工模块都已批准；另外 71 个旧模块没有标记，判不了。这和人工核对一致。
