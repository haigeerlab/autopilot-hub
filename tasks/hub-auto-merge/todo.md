# Todo: hub-auto-merge

测试命令：`scripts/validate.sh --quick`（日常）；`scripts/validate.sh && PYTHON=/usr/bin/python3 scripts/validate.sh`（推送前）

- [x] Task 1：风险分级按 R4
  - 验证：`tests.test_risk`、`tests.test_admission`
  - 文件：`risk/templates.json`、`risk/tiers.py`、`admission/decide.py`、`.github/autopr/risk-tiers.json`、对应测试
- [x] Task 2：`orchestrator/state.py`
  - 验证：R13（`tests.test_state`）
  - 文件：`orchestrator/state.py`、`tests/test_state.py`
- [x] Task 3：run 的路线与 `autopilot/verified`
  - 验证：R2、R3、R4、R11（RunFailureRecordTest）、R12（DirectModeTest）
  - 文件：`orchestrator/flow.py`、`orchestrator/cli.py`、`tests/test_orchestrator.py`
- [x] Task 4：automerge 路线与回退
  - 验证：R1、R6
  - 文件：`orchestrator/flow.py`、`orchestrator/cli.py`、`tests/test_orchestrator.py`
- [x] Task 5：修复上限与「不许改裁判」
  - 验证：R9
  - 文件：`orchestrator/flow.py`、`orchestrator/state.py`、`tests/test_orchestrator.py`
- [x] Checkpoint 1（report）：R1～R4、R6、R9、R16 通过（2026-11-27 按回归文档命令逐条执行全部 OK；回归文档命令加 PYTHONPATH=tests 前缀，见其变更记录）
- [x] Checkpoint 2（gate）：推送并开实现 PR 一（Task 1～5），用户说「合并」（2026-11-27 hub#96 用户「合并」，已合 2806aa4）
- [x] Task 6：ai-operate 交 delegate
  - 验证：R7
  - 文件：`verify/core.py`、`orchestrator/flow.py`、`orchestrator/cli.py`、`tests/test_verify.py`、`tests/test_orchestrator.py`
- [x] Task 7：`orchestrator/classify.py`
  - 验证：R8（`tests.test_classify`）
  - 文件：`orchestrator/classify.py`、`tests/test_classify.py`
- [x] Task 8：`orchestrator/watch.py` 与 `watch`、`report-failure` 子命令
  - 验证：R5、R8、R10、R11、R12、R13
  - 文件：`orchestrator/watch.py`、`orchestrator/cli.py`、`tests/test_watch.py`
- [x] Task 9：degrade 的 `skip_main_red`
  - 验证：R18
  - 文件：`.github/workflows/degrade.yml`、`degrade/run.py`、`tests/test_degrade.py`
- [x] Task 10：launchd 模板、README、CHANGELOG
  - 验证：R16、R17
  - 文件：`examples/launchd/com.haigeerlab.autopilot-watch.plist`、`README.md`、`README.en.md`、`CHANGELOG.md`
- [x] Checkpoint 3（report）：R5、R7、R8、R10～R13、R18 通过（2026-11-27 按回归文档命令逐条执行全部 OK，全量两遍通过）
- [ ] Checkpoint 4（gate）：推送并开实现 PR 二（Task 6～10），用户说「合并」
- [ ] Checkpoint 5（gate）：最终验收：沙盒 R14、R15 等用户原话
