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

基线现在是空的。agent-relay 开"只接管 Plan"（agent-relay#194）合并后，再单独开一个 T4 PR 登记它。
