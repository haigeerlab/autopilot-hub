# Spec：切片 9，硬卡证据（第二批）

> 2026-11-22，autopr-design。总账 `research/2026-11-18-自动PR系统设计/batch-2-auto-merge-spec.md`（第二批规格，用户已批准，原话"批准"）的切片 9，对应假设 B5–B7。
> 上游：合并方案 r3 的 Q2（最终验收凭三样证据）、8.1 安全内核（两条硬卡、证据绑定）。

## 证据评论格式

本机编排（切片 10）跑完检查和审查后，以机器人身份发一条新评论：

```
<!-- autopilot-evidence:v1 {
  "head_sha": "<40 位当前 PR head>",
  "runner": "autopilot-orchestrator",
  "checks": [{"cmd": "<命令>", "exit": 0, "output_sha256": "<输出的 sha256>"}],
  "review": {"backend": "codex", "status": "completed", "fingerprint": "<delegate 报告指纹>", "blocking": 0, "rounds": 1},
  "acceptance": {"marker": "codex-approved" | "approved", "sha": "<40 位>"} | null
} -->
```

评论正文另附可读版：每条命令和输出的最后 40 行，供人查看。准入只读机器可读块。

## 准入怎么判 gate

1. hub 没配机器人身份 → 不通过。
2. 不知道当前 head SHA → 不通过（workflow 传 `--head-sha`）。
3. 只看机器人发的、没被编辑过的证据评论（判据同切片 8）。不合格的忽略，摘要里计数。
4. 只看 `head_sha` 等于当前 head 的证据。别的提交上的证据不算，摘要里写"有 N 条证据绑定的不是当前提交"。PR 一有新推送，旧证据自动失效。
5. 同一提交上有多条证据时，以最新一条为准。下面几项都满足才通过：
   - `checks` 不为空，每条都有命令，退出码为整数 0；
   - `review.status == completed`，有 `fingerprint`，`blocking` 为整数 0；
   - 接管评论里写了 spec-guard 模块（`module` 非空）时，还要 `acceptance.marker` 为 `codex-approved` 或 `approved`，且 `acceptance.sha` 等于当前 head（Q2 最终验收）。没有模块的仓库（如 agent-relay 的普通修复）跳过验收这一项（B7）。
6. 不通过时，摘要里逐条列出原因，例如"检查 `make test` 退出码 2"、"审查还有 1 条 blocking"。

"非作者"由谁保证：证据只认机器人发的，而机器人私钥只在本机编排手里，写代码的会话拿不到。检查由编排在临时克隆里重新跑，不采信写代码那一方自己报的结果。

## 不变的

风险档、接管、降级三项照旧；检查结果仍是 `neutral`。四项都满足时，标题第一次可能写"可以自动合并"，但仍然只报告。真正合并的是切片 10 的本机编排，它合并前会再读一次准入结论。
