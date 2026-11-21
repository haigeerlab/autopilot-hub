# Spec：切片 7，退役 repo-readiness 依赖，准入改用新判定（第一批收尾）

> 2026-11-21，autopr-design。用户要求写退役规格（原话"合并 #15，然后写退役规格"）。用户已批准本规格与 A1–A8（原话"批准"）。
> 上游：总账 `research/2026-11-18-自动PR系统设计/`：合并方案 r3 第 6 节"迁移与退役步骤"、8.1 安全内核；方向文档五之二（repo-readiness 封版）；readiness-tool #21851 的依赖清单。

## 0. 假设（请先确认）

| # | 假设 | 不成立时 |
| --- | --- | --- |
| A1 | **准入仍然只报告**，但不再报"信任上限 Lx"。标题改成一句结论：**能不能自动合并**，以及为什么。判断依据四项：风险档（切片 4）、接管档位里的"合并"项、硬卡证据、未关闭的降级 issue | 退役后先保留 L 级显示一段时间 |
| A2 | **等级评论换格式**：旧的 `autopilot-levels:v1` 装的是已退役的 strict／standard／relaxed，改为 `autopilot-takeover:v1 {"module": ..., "spec": ..., "plan": ..., "merge": "human"\|"auto", "release": ...}`，由第二批的本机编排按 spec-guard 0.91 的 `/map --json` 写入。**没有这条评论时，合并按"人合"处理** | 继续读旧格式 |
| A3 | **降级 issue 不再表示 L 级**：只要有一个未关闭的降级 issue，就"不会自动合并"，并在理由里列出。降级 issue 正文里的 `level` 字段保留（兼容已开的 issue），但不再参与计算；正文措辞改成"在本 issue 关闭之前，这个仓库不会自动合并" | 继续按 L 级算 |
| A4 | **硬卡证据第二批才有**：非作者在最终提交上跑检查的证据评论还没做，所以现在每个 PR 都会显示"硬卡：还没有证据"，结论都是"不会自动合并"。这符合"只报告"阶段的预期 | — |
| A5 | **标签**：不再打 `autopilot:cap-Lx`，改打 `autopilot:risk-T0`…`T4`；已有 PR 上的旧标签不追删 | 两种都打一段时间 |
| A6 | **风险配置改回 `.yml`**（切片 4 的 A3 约定）：切片 5 已搬来 YAML 子集解析器，准入先读 `.github/autopr/risk-tiers.yml`，没有再读 `.json`；两份都在时以 `.yml` 为准并在摘要里提醒"两份不一致"（若不一致） | 只认 `.json` |
| A7 | **切片 3（每周掉档复检）整块下线**：删代码、示例、规格标为已退役；你本机的 `~/.autopilot/recheck/`（含配置、基线、临时克隆，定时任务早已停）由我在你同意后删除 | 保留代码不用 |
| A8 | **顺手修两处自己造成的误报**：`risk/tiers.py` 的一行代码注释和一个测试字符串里写到了压制标记，让 PR #15 的 `suppression-budget` 失败；改写措辞 | 留着 |

## 1. 要删的（hub）

- `schemas/vendor/repo-readiness/`（结论格式与来源记录）、`allowed-tools.json`、`tier-map.json`；
- `admission/decide.py` 里读结论的部分：结论校验（含迷你 JSON Schema 校验器）、`tool_allowed`、`app_cap`、`valid_until`、`plugin_limits`、L 级相关；`glob_match` 保留（风险分级、体检在用）；
- `recheck/` 整个目录、`examples/recheck-config.example.json`、`examples/com.haigeerlab.autopilot-recheck.plist`、`tests/test_recheck.py`；
- `degrade/run.py` 里的 `readiness-drop` 原因文案；
- 测试夹具：`conclusion.basic.json`、`conclusion.agent-relay-m1.json`、`allowed-tools.json`；相关测试改写或删除；
- `.github/workflows/admission.yml` 里取接入结论、传 schema、允许列表、档位对应表的步骤。

## 2. 新的准入输出

```
autopilot/admission：不会自动合并（风险档 T4；接管档位：合并由人；硬卡：还没有证据）

| 项 | 结果 | 说明 |
| risk | ⛔ | 风险档 T4，停下交你：… |
| takeover | — | 没有接管评论，合并按"人合"处理 |
| gate | — | 硬卡：还没有"非作者在最终提交上跑过检查"的证据（第二批） |
| degrade | ✅ | 没有未关闭的降级 issue |
```

结论规则：**四项都满足才写"可以自动合并"**——风险档 T0–T2、接管档位的合并为 `auto`、硬卡有证据且绑定当前提交、没有未关闭的降级 issue。任何一项不满足，写"不会自动合并"并列出原因。检查结果仍是 `neutral`。

## 3. hub 以外

| 谁 | 做什么 |
| --- | --- |
| ecosystem-lead → agent-relay-owner | 删 `.github/autopr/readiness-conclusion.json`；两个调用文件的钉升到本切片合并后的 hub 提交（T4，用户合并） |
| 你（或我，经你同意） | 删本机 `~/.autopilot/recheck/` |
| repo-readiness | 不动：标签停在 v0.3.1，仓库保留 |
| agent-relay#186 | 演练开的 `readiness-drop` 降级 issue，关闭（经 ecosystem-lead 或你） |

## 4. 验收标准

1. 全部测试在 3.9 和 3.10 上通过；新增：四项判定各自"满足／不满足"、没有接管评论按人合、未关闭降级 issue 阻止自动合并、`.yml` 优先于 `.json`。
2. 仓库里搜不到 `readiness-conclusion`、`allowed-tools`、`tier-map`、`trust_cap`、`autopilot-levels`（规格文档里的历史记录除外）。
3. 本 PR 上的准入评论是新格式；`suppression-budget` 在本 PR 上通过。
4. 回放 hub #11–#15、agent-relay 最近几个 PR，新准入结论都是"不会自动合并"，理由正确。

## 5. 依赖与分工

| 谁 | 做什么 |
| --- | --- |
| autopr-design | 规格、实现、测试、hub 的 PR、回放 |
| 你 | 批准本规格（含 A1–A8）；合并 hub 的 PR；同意删本机目录 |
| ecosystem-lead | 转交第 3 节 agent-relay 的两件事 |
