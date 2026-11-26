# 回归测试文档：hub-auto-merge

> 对应需求 `spec/hub-auto-merge.md` 的成功标准 S1～S13 与技术方案 `tasks/hub-auto-merge/design.md` 的「验收方式」。命令用例的测试类在实现时按这里的名字建；沙盒端到端用例由用户执行并回复原话（作者不能自验 GitHub 侧行为）。

## 用例清单

### R1: 满足全部条件时打开自动合并且先打 authored
- 步骤：用假 IO 构造新流程模块、档位 auto、非 T4、准入通过、主干规则要求来源为 App 的 `autopilot/verified` 且要求同步、watch 心跳新鲜的 PR，调 `flow.run`
- 预期：写 `autopilot/verified=success`；`watch-state` 先记 `automerge-requested`；`autopilot:authored` 标签在 `enablePullRequestAutoMerge` 之前；调用带 `expectedHeadOid=validatedHeadOid`；结果 `automerge-on`，退出码 0
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_orchestrator.AutoMergeRouteTest -v`
- 证据：运行输出

### R2: 缺任一部署前提时走人工路
- 步骤：对 R1 的场景逐项去掉一个前提（主干规则没有该状态、来源不是 App、没要求同步、watch 心跳过期、`mergeMode=direct`、仓库已收紧、已冻结）
- 预期：每种都不打开自动合并；前六种 `route=manual` 并在证据评论写明原因、结果 `handed-to-user`；`mergeMode=direct` 时 `route=direct` 走原直接合并
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_orchestrator.RoutePreconditionTest -v`
- 证据：运行输出

### R3: T4、老流程、Dependabot 拿到状态但交用户
- 步骤：分别构造改 `.github/workflows/` 的 PR、老流程模块（档位不是 auto、最终验收已记录）、`dependabot[bot]` 开的 PR，调 `flow.run`
- 预期：三者都写 `autopilot/verified=success`、都不打开自动合并、都在 PR 上评论「由你合并」并通知会话；老流程不在档位处提前停；Dependabot 不读模块、不跑回归用例
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_orchestrator.ManualRouteTest -v`
- 证据：运行输出

### R4: 验证不通过写 failure、不打开、不合并
- 步骤：让一条回归用例失败；再构造同一提交此前已有 success 且已排队自动合并的情形
- 预期：写 `autopilot/verified=failure`；没有打开自动合并、没有合并；已排队的自动合并被关闭
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_orchestrator.VerificationFailureStatusTest -v`
- 证据：运行输出

### R5: 打开后 head 变了由 watch 关闭
- 步骤：`watch-state` 里有 `automerge-on` 的 PR，假 GitHub 返回新 head（或该 head 上状态不是 success），跑 `watch --once`
- 预期：调用 `disablePullRequestAutoMerge`；PR 进入 `paused`（恢复条件：重新 run 通过）；`outbox` 有给 `notify` 会话的消息
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_watch.HeadChangedTest -v`
- 证据：运行输出

### R6: 打开被拒时的回退只在核对无误时直接合并
- 步骤：让 `enablePullRequestAutoMerge` 分别返回 head 对不上（之后对上／仍对不上）和其他拒绝（必需检查全成功／有未成功／来源不是 App／PR 落后主干）
- 预期：head 对不上时最多重试 4 次，对上后再试一次；其他拒绝时只有 head、必需检查及来源、未落后三者都对才 `PUT .../merge` 并同步收尾；其余都暂停、不合并
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_orchestrator.AutoMergeFallbackTest -v`
- 证据：运行输出

### R7: ai-operate 用例交 delegate 执行，不可用时转人工
- 步骤：回归测试文档里有一条 `ai-operate` 用例；假 delegate 分别返回 pass、fail、解析不到、不可用；会话分别传与不传 `--author-host`
- 预期：pass／fail 写进验证报告的执行记录（执行者 `delegate:<后端>`、证据）；后端选非作者一方；解析不到、不可用、没传作者宿主时该用例转人工组并在跟踪 issue 注明原因；每条用例用独立的一次性克隆且跑完删除
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_verify.AiOperateTest tests.test_orchestrator.AiOperateFlowTest -v`
- 证据：运行输出

### R8: CI 失败三分类
- 步骤：给分类器喂三组 job／step 数据：setup 步骤（排在测试 step 之前）失败、`startup_failure`、测试 step 失败；另喂一组判不准的
- 预期：依次判为 `env`、`config`、`test`、`test`；watch 对 `env` 调 `rerun-failed-jobs` 最多两次、两次后仍红进入暂停并通知用户；对 `config` 暂停并通知用户和会话、不收紧；对 PR 上的 `test` 通知会话
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_classify tests.test_watch.PrCiFailureTest -v`
- 证据：运行输出

### R9: 修复上限与「不许改裁判」
- 步骤：同一回归用例在提交 A、B、C 上连续失败（每次都是新提交、重跑过）；另构造改了基线已有测试文件、改了 `tasks/<id>/regression.md` 的 PR
- 预期：A 失败后 B 再失败记 1 次、C 再失败记 2 次，之后进入暂停并通知用户（附两个提交和可选做法）；同一提交不重复计数；改了已有测试或回归文档立即暂停
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_orchestrator.RepairLimitTest -v`
- 证据：运行输出

### R10: 主干测试失败的收紧顺序
- 步骤：仓库 `watchMainRed=true`，主干一次 push 运行测试失败（含该仓库有两个排队中的自动合并），跑 `watch --once`；之后对该仓库的 PR 调 `run` 通过；再执行 `watch --resume <repo>`
- 预期：先写 `repos.narrowed`，再关掉两个排队的自动合并，再开降级 issue、调 `narrow_pending`，最后通知用户；没有撤回、没有触发发版；`run` 通过后路线仍为 `manual`；`--resume` 后才恢复
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_watch.MainRedTest -v`
- 证据：运行输出

### R11: 各种出错和超时都进入暂停
- 步骤：逐个模拟：自动合并打开超过 60 分钟未合、PR 上单次 CI 超过 60 分钟、主干单次运行超过 60 分钟、`report-failure --stage open-pr`、`run` 内 `ApiError`、watch 一步失败
- 预期：每种都有 `paused` 记录（原因、时间、恢复条件）和待发通知；`ApiError` 时 `run` 退出码 2；不空等、不跳过
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_watch.TimeoutAndFailureTest tests.test_orchestrator.RunFailureRecordTest -v`
- 证据：运行输出

### R12: 回滚三步
- 步骤：`watch --freeze` 后跑一轮；再跑一轮看排队中的自动合并；把 `mergeMode` 设为 `direct` 后 `run --dry-run`
- 预期：冻结后没有新的打开事件，`run` 路线为 `manual`；排队的自动合并全部被关闭；dry-run 输出 `route=direct`
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_watch.FreezeTest tests.test_orchestrator.DirectModeTest -v`
- 证据：运行输出

### R13: 停过一段时间后接着处理且不重复
- 步骤：`watch-state` 的游标停在某次运行；假 GitHub 上此后有两次主干运行（其中一次重跑过）和一个已合并的 PR；连续跑两轮 `watch --once`；另模拟上一轮在「打开自动合并」后崩溃
- 预期：两次运行（按运行编号和重跑次数）都处理；跟进 issue 只开一次；通知不重复；崩溃那一项按远端状态对账，不重复打开
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_state tests.test_watch.ResumeTest -v`
- 证据：运行输出

### R14: 沙盒端到端：自动路合并与主干 CI
- 步骤：沙盒按 B 批设好主干规则（必需检查 `autopilot/verified` 来源限定 App、要求同步）、装好 watch；新流程模块开一个非 T4 的 PR，会话调一次 `hub run`；等 GitHub 合并后再等一轮 watch
- 预期：PR 时间线上 authored 早于自动合并；GitHub 合并；主干树等于 `validatedHeadOid` 的树；watch 打 `autopilot:auto-merged`、开跟进 issue；主干 push CI 跑了；期间本机没有进程在等
- 执行方式：manual
- 证据：用户原话（附 PR 链接）

### R15: 沙盒端到端：推新提交、主干变红
- 步骤：在沙盒 R14 的设置下，①打开自动合并后往 PR 推一个提交；②合进一个会让主干测试失败的改动
- 预期：①GitHub 不合，下一轮 watch 关掉自动合并并给会话发消息；②watch 写收紧、关队列、开降级 issue 和收窄 PR、用户收到 macOS 通知；没有撤回
- 执行方式：manual
- 证据：用户原话（附 PR、issue 链接）

## 成功标准对照

| 成功标准 | 用例 |
|---|---|
| S1 | R1, R14 |
| S2 | R2 |
| S3 | R3 |
| S4 | R4 |
| S5 | R5, R15 |
| S6 | R6 |
| S7 | R7 |
| S8 | R8 |
| S9 | R9 |
| S10 | R10, R15 |
| S11 | R11 |
| S12 | R12 |
| S13 | R13 |

## 受影响的老功能

### R16: 原有编排、准入、风险、降级、跟进测试仍通过
- 步骤：跑原有的编排、批处理、跟进、准入、风险、降级、验证测试
- 预期：除按本模块有意改变的断言（已在同一 PR 里改并说明）外全部通过
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_orchestrator tests.test_batch tests.test_followups tests.test_admission tests.test_risk tests.test_degrade tests.test_verify -v`
- 证据：运行输出

### R17: 全量检查两遍
- 步骤：在仓库根目录跑全量检查，默认 Python 与 macOS 自带 3.9 各一遍
- 预期：两遍都退出 0
- 执行方式：script
- 命令：`scripts/validate.sh && PYTHON=/usr/bin/python3 scripts/validate.sh`
- 证据：运行输出

### R18: degrade 旧入口保留
- 步骤：`skip_main_red` 为 false 与 true 两种输入下，分别喂主 CI 失败、撤回提交、回归标签、冻结标签、非管理员关闭降级 issue 五种事件
- 预期：false 时五种行为与改动前相同；true 时只有主 CI 失败被跳过，其余四种不变
- 执行方式：command
- 命令：`python3 -B -m unittest tests.test_degrade.SkipMainRedTest -v`
- 证据：运行输出
