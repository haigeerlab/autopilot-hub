# Plan：hub-auto-merge

> 依据 `spec/hub-auto-merge.md`（已批）、`tasks/hub-auto-merge/design.md`（已批）、`tasks/hub-auto-merge/regression.md`。实现 task 一次一个派 Codex 写（`delegate run --write`），autopr-design 验收（看基线、`git status`、`git diff --stat`，自己重跑测试）、提交；代码审查用 `delegate review --backend claude-code`（同厂商审查）。每个 task 日常跑 `scripts/validate.sh --quick`，推送前跑全量两遍。

## 任务

- Task 1：风险分级按 R4：`risk/templates.json` 的 T4 清单与默认档、`risk/tiers.py` 的 spec/tasks 规则与 `human_only`、`admission/decide.py` 风险项、hub 自己的 `.github/autopr/risk-tiers.json`；同步改 `tests/test_risk.py`、`tests/test_admission.py` 里有意改变的断言。验证：`python3 -B -m unittest tests.test_risk tests.test_admission -v` 通过；R16 中这两个模块通过。
- Task 2：`orchestrator/state.py`：文件锁、原子写、意图与幂等记录、`outbox`、`repairs`、`repos.narrowed`、`frozen`、心跳；`tests/test_state.py`。验证：R13 中 `tests.test_state` 通过。
- Task 3：run 的路线与 `autopilot/verified`：停点改为记路线、成功写 success／失败写 failure 并关队列、路线判定（direct、冻结／收紧、七项前提含 App 来源与同步）、manual 路线评论与通知、老流程不提前停、Dependabot 只检查路线、结果状态与退出码、`run --notify --author-host`、令牌按命令分组；补 `tests/test_orchestrator.py`。验证：R2、R3、R4、R11 中 `RunFailureRecordTest`、R12 中 `DirectModeTest` 通过。
- Task 4：automerge 路线：先记意图、先打 authored、`enablePullRequestAutoMerge(expectedHeadOid)`、head 对不上重试、被拒后核对再直接合并并同步收尾。验证：R1、R6 通过。
- Task 5：修复上限与「不许改裁判」：原因键、按新提交计次、两次后暂停、改已有测试或回归文档即暂停（测试路径复用模板）。验证：R9 通过。
- Task 6：ai-operate 交 delegate：`verify/core.py` 加 `ai` 组与执行记录、`flow` 里一次性克隆执行与转人工。验证：R7 通过。
- Task 7：`orchestrator/classify.py` 与 `tests/test_classify.py`。验证：R8 中 `tests.test_classify` 通过。
- Task 8：`orchestrator/watch.py` 与 `cli.py watch`、`report-failure`：冻结、PR 巡检（head、状态、合并后收尾复用 followups 证据、60 分钟超时、PR CI 分类与重跑）、主干逐条处理与收紧顺序、通知发送（macOS 通知、agent-relay 脚本发送）、心跳与游标、`--resume`；`tests/test_watch.py`。验证：R5、R8、R10、R11、R12、R13 通过。
- Task 9：`degrade.yml` 输入 `skip_main_red`、`degrade/run.py` 按输入跳过主 CI 失败分支；`tests/test_degrade.py`。验证：R18 通过。
- Task 10：`examples/launchd/com.haigeerlab.autopilot-watch.plist`、README（中英）现状与安装卸载、「打开自动路之前」逐仓库清单、CHANGELOG。验证：R16、R17 通过。

## 检查点

- Checkpoint 1（report）：Task 1～5 完成，R1～R4、R6、R9 及 R16 通过，结果记入 todo 后继续。
- Checkpoint 2（gate）：推送分支并开第一个实现 PR（Task 1～5），由用户说「合并」。
- Checkpoint 3（report）：Task 6～9 完成，R5、R7、R8、R10～R13、R18 通过，结果记入 todo 后继续。
- Checkpoint 4（gate）：推送分支并开第二个实现 PR（Task 6～10），由用户说「合并」。
- Checkpoint 5（gate）：最终验收：沙盒按 B 批设好后跑 R14、R15，等用户原话；R16、R17 在合并前跑过。
