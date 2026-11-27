# Plan：hub-release

> 依据 `spec/hub-release.md`（已批）、`tasks/hub-release/design.md`（已批）、`tasks/hub-release/regression.md`。实现 task 一次一个派 Codex 写（`delegate run --write`，派前问用户），autopr-design 验收（看基线、`git status`、`git diff --stat`、真实 diff，自己重跑测试）、提交；代码审查用 `delegate review --backend claude-code`（同厂商审查）。每个 task 日常跑 `scripts/validate.sh --quick`，推送前跑全量两遍。

## 任务

- Task 1：`pipeline/release_gate.py` 的发布范围、模块归属与需求分组（技术方案第 2 节前半）：上次发版提交、先后判定、范围内 PR（含集成分支带入的内层 PR）、可信 v2 证据、按 `overallDesign` 分组与落地证据、没有总体方案时从严；`tests/test_release_gate.py`。验证：R1。
- Task 2：`release_gate.py` 的路线判定、版本号计算与确认解析（第 2 节后半、第 3、4、5 节的纯函数部分）：入口可用性（含 `plugin-auto` 模板、`install-local`／`verify-install` 不可移除、`release-tag.yml`、tag 规则集）、作废、人批优先；按 PR 标题算版本；确认与继续指令解析。验证：R2、R3、R4。
- Task 3：`pipeline/limits.py` 的 `authorized` 参数与 v2 读取，`engine.final_level`；代码不内置放宽值，`release-limits.json` 本 task 不改。验证：R5。
- Task 4：引擎新入口：`engine.run` 加 `target`／`tag`，两条入口并存与名单（无 v2 时内置名单）、仓库名规范化、资格核对（主干 push CI、必需检查与来源）、新模板 `pipeline/templates/plugin-auto.yml`、每次发清单与确认块、`cli.py release --target/--tag`。验证：R9 中 `TargetOrderTest`、R16。
- Task 5：守卫步骤：`tag-and-release` 隔离执行与引用快照核对、`push-tag`（克隆 B、按对象 id、`RELEASE_TAG` 令牌、推前重核与远端对账、`pushedAt` 先记）、`await-release`（`RELEASE_READ` 令牌、运行与 Release 核对、`released` 与 `ready`）。验证：R6、R7、R8。
- Task 6：状态与恢复：`watch-state.json` 的 `releases`、`released`、`lastReleasedSha`，意图与幂等键、每仓库发版锁与认领、本机三步中断后的继续、`finishing` 逐项收尾。验证：R11 中 `RecoveryTest`。
- Task 7：watch 接入：候选入队与留队重查、按祖先关系选候选、启动发版子进程、开工前／进行中守卫、按工作流解除主干暂停（`runs` 补工作流 id）、读 `waiting-human`／`paused` 的继续指令、`watch --resume` 清仓库级巡检暂停。验证：R9 中 `ReleaseQueueTest`、R10、R11 中 `ReleaseResumeTest`、R17。
- Task 8：人批交接与通知：`--tag` 交接流程、开始前汇报与会话通知、发版后问题通知、`report-failure --stage release --version`。验证：R12、R13。
- Task 9：文档：README（中英）发版现状与「打开自动发版之前」清单（含交接清单各项）、CHANGELOG、`CLAUDE.md`／`AGENTS.md` 职责边界 hub 两句（已批措辞）。验证：R18。
- Task 10：`release-limits.json` 升 v2：`release_repos`、`authorized_cap: "auto-confirm"`、`release_creator`（spec-guard 为 `local`，补上 `release-tag.yml` 后改掉）。T4，单独最后一个实现 PR。验证：R5、R18。

## 检查点

- Checkpoint 1（report）：Task 1～3 完成，R1～R5 通过，结果记入 todo 后继续。
- Checkpoint 2（gate）：Task 4～5 完成，R6～R9 中引擎部分、R16 通过；推送并开第一个实现 PR（Task 1～5），由用户说「合并」。
- Checkpoint 3（report）：Task 6～8 完成，R9～R13、R17 通过，结果记入 todo 后继续。
- Checkpoint 4（gate）：Task 9 完成、R18 两遍通过；推送并开第二个实现 PR（Task 6～9），由用户说「合并」。
- Checkpoint 5（gate）：交接清单在沙盒落地后开第三个实现 PR（Task 10），由用户说「合并」。
- Checkpoint 6（gate）：最终验收：沙盒 R14、R15，逐仓 R19，等用户原话。
