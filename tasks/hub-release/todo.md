# Todo: hub-release

测试命令：`scripts/validate.sh --quick`（日常）；`scripts/validate.sh && PYTHON=/usr/bin/python3 scripts/validate.sh`（推送前）

- [ ] Task 1：发布范围、模块归属与需求分组
  - 验证：R1
  - 文件：`pipeline/release_gate.py`、`tests/test_release_gate.py`
- [ ] Task 2：路线判定、版本号与确认解析
  - 验证：R2、R3、R4
  - 文件：`pipeline/release_gate.py`、`tests/test_release_gate.py`
- [ ] Task 3：上限的授权放宽与最终档位
  - 验证：R5
  - 文件：`pipeline/limits.py`、`pipeline/engine.py`、`tests/test_limits.py`、`tests/test_engine.py`
- [ ] Checkpoint 1（report）：R1～R5 通过
- [ ] Task 4：引擎新入口、资格、新模板与清单
  - 验证：R9（TargetOrderTest）、R16
  - 文件：`pipeline/engine.py`、`pipeline/templates/plugin-auto.yml`、`orchestrator/cli.py`、`tests/test_engine.py`、`tests/test_pipeline.py`
- [ ] Task 5：建 tag、推 tag、等 Release 守卫步骤
  - 验证：R6、R7、R8
  - 文件：`pipeline/engine.py`、`orchestrator/cli.py`、`orchestrator/token.py`、`tests/test_engine.py`、`tests/test_release_checkout.py`
- [ ] Checkpoint 2（gate）：推送并开实现 PR 一（Task 1～5），用户说「合并」
- [ ] Task 6：发版状态、锁、幂等与收尾
  - 验证：R11（RecoveryTest）
  - 文件：`orchestrator/state.py`、`pipeline/engine.py`、`tests/test_state.py`、`tests/test_engine.py`
- [ ] Task 7：watch 接入
  - 验证：R9（ReleaseQueueTest）、R10、R11（ReleaseResumeTest）、R17
  - 文件：`orchestrator/watch.py`、`orchestrator/state.py`、`orchestrator/cli.py`、`tests/test_watch.py`
- [ ] Task 8：人批交接与通知
  - 验证：R12、R13
  - 文件：`pipeline/engine.py`、`orchestrator/cli.py`、`orchestrator/watch.py`、`tests/test_engine.py`、`tests/test_watch.py`
- [ ] Checkpoint 3（report）：R9～R13、R17 通过
- [ ] Task 9：README、CHANGELOG、职责边界
  - 验证：R18
  - 文件：`README.md`、`README.en.md`、`CHANGELOG.md`、`CLAUDE.md`、`AGENTS.md`
- [ ] Checkpoint 4（gate）：推送并开实现 PR 二（Task 6～9），用户说「合并」
- [ ] Task 10：`release-limits.json` 升 v2
  - 验证：R5、R18
  - 文件：`release-limits.json`、`tests/test_limits.py`
- [ ] Checkpoint 5（gate）：交接清单在沙盒落地后开实现 PR 三（Task 10），用户说「合并」
- [ ] Checkpoint 6（gate）：最终验收：沙盒 R14、R15，逐仓 R19，等用户原话

## Skill 使用记录

- 需求与技术方案：spec-driven-development（复述、假设先确认）；外部审查用 delegate:review。
