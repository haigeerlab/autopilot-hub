# 回归测试文档：hub-release

> 对应需求 `spec/hub-release.md` 的成功标准 S1～S16 与技术方案 `tasks/hub-release/design.md` 的「验收方式」。命令用例的测试类在实现时按这里的名字建，全部用假 GitHub 接口、假克隆和临时目录，不发真实请求、不推真实 tag；沙盒与逐仓端到端用例由用户执行并回复原话。

## 用例清单

### R1: 发布范围、模块归属与需求分组
- 步骤：构造上次发版提交到目标之间的提交与 PR：普通 squash PR、merge commit、经集成分支带入的内层 PR、找不到 PR 的提交、只改 `spec/`／`tasks/` 的 PR；同一总体方案的两个模块（一个已完成并有落地证据、一个没有）；没有总体方案的模块且能力图上另有未完成模块
- 预期：内层 PR 计入范围；找不到 PR 或可信证据的记「未归属」；同组成员缺落地证据时本次不发；纯文档 PR 不算落地；没有总体方案且另有未完成模块时路线至少 `confirm`，清单列出这些模块
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_release_gate.ScopeAndGroupTest -v`
- 证据：运行输出

### R2: 路线判定的先后与作废
- 步骤：逐项构造：任一模块发版档位 human；档位被收窄到 `prepare`；`releasePreauth` 为假／缺字段；改到 T4；升主版本；`scope_base` 取不到或不是目标祖先；中途修了两次；入口没登记、模板 `tag-and-release` 被替换、`install-local` 被移除、没有 `release-tag.yml`、`release_creator` 为 local、tag 规则集缺失或放行了其他身份
- 预期：人批优先；其余作废或缺授权一律 `confirm`；修两次不影响；入口、模板、规则集不满足一律 `human`；全部满足才 `auto`；判定原因写进结果
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_release_gate.RouteTest -v`
- 证据：运行输出

### R3: 版本号计算
- 步骤：范围内 PR 标题分别含 `!`、正文含 `BREAKING CHANGE:`、`feat`、`fix`；多个 PR 混合；有标题不合 F1；版本 tag 有 `v1-nightly` 这类非版本 tag；命令行给了不一致的 `--version`
- 预期：分别升主、主、次、修订；多个 PR 取最高一级、只升一次；不合 F1 的按修订号计并让路线至少 `confirm`；非版本 tag 被忽略；`--version` 不一致时停
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_release_gate.VersionTest -v`
- 证据：运行输出

### R4: 确认解析
- 步骤：所有者发的有效确认（单独一行、附说明）；引用里的确认（含没有 `>` 的续行）；代码块和缩进行里的确认；指纹不符；编辑过（GraphQL `lastEditedAt` 非空）；非所有者；组织仓库；清单换了 `seq` 后的旧确认；`继续发版 <seq> 从 <步骤>`
- 预期：只有段首、未编辑、所有者、指纹对得上的确认成立；其余都不算；组织仓库停下；旧确认在清单变化后作废；继续指令只接受中断那步或其后一步
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_release_gate.ConfirmParseTest -v`
- 证据：运行输出

### R5: 最终档位与上限
- 步骤：`auto`、`confirm` 未确认／已确认、人批交接前后，分别配上限文件 v1、v2 有／无 `authorized_cap`、缺失、非法；`pipeline_own` 与 kb；显式 `human` 上限
- 预期：未授权时与现在完全一致；只有授权为真且 v2 给了 `authorized_cap` 时 `pipeline_own` 放宽到 `auto-confirm`；`repo_caps` 仍取低；文件缺失、非法、v1 时试点仓库最高 `prepare`；`human` 上限照样压住；档位不足时该步标 `needsUser` 并写原因
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_limits.AuthorizedCapTest tests.test_engine.FinalLevelTest -v`
- 证据：运行输出

### R6: 本地建 tag 的隔离与核对
- 步骤：在假克隆里执行登记的 `release_publish`：正常建一个附注 tag；多建一个 tag；移动一个旧 tag；建轻量 tag；指向别的提交；检查执行环境里的凭据变量与 git 配置
- 预期：执行环境去掉了 `GH_*`、`GITHUB_*`、`GIT_*` 凭据类变量与 `SSH_AUTH_SOCK`，`HOME` 为临时目录，git 不读全局与系统配置；只有正常情形通过，其余都停下
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_engine.TagLocalTest -v`
- 证据：运行输出

### R7: 推 tag 的授权复核与远端对账
- 步骤：自动路线与确认路线分别推 tag：推前授权已失效（规则集被关、确认评论被编辑）；远端已有同名 tag 指向目标；远端同名 tag 指向别处；推送中断后恢复（本地 tag 丢失）
- 预期：推前重算判定、重读原评论；授权失效时不推；远端已对就跳过并补记；指向别处时停下；本地丢失时重建；只在不跑仓库代码的克隆里按对象 id 推一个引用，禁用钩子；推送令牌只含 `contents: write`，用完撤销；`pushedAt` 在推送前写入意图
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_engine.PushTagTest -v`
- 证据：运行输出

### R8: 等 Release 就绪
- 步骤：`release-tag.yml` 运行成功且 Release 匹配；运行失败但 Release 已建；运行没出现；超过 30 分钟；Release 名字不含版本；tag 指向别处；人批交接（不按时间筛）；`release_creator` 为 local
- 预期：只有第一种继续，并把运行 id、attempt、Release 链接记进 `released`、置 `ready`；其余停下；local 仓库只核 Release 与 tag
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_engine.AwaitReleaseTest -v`
- 证据：运行输出

### R9: 目标固定、顺序、锁与候选
- 步骤：新发版与续跑时主干又前进；目标是上次的祖先、相等、分叉；首次发版（没有版本 tag）；人批交接用旧 tag；两个进程同时认领；大小写不同的仓库名；候选里有互为祖先和不完整的；`waiting-human`／`paused`／`finishing` 时来了新候选；旧入口在新流程发版进行中被调用
- 预期：始终只发固定目标；祖先或相等标 `covered`，分叉停下；首次发版人批；交接不把本次 tag 当上次；同一仓库只一个认领；仓库名规范化后共用锁和状态；选是其他候选后继的那个；等待、暂停、收尾中的发版不被新候选覆盖；旧入口被拒
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_engine.TargetOrderTest tests.test_watch.ReleaseQueueTest -v`
- 证据：运行输出

### R10: 守卫
- 步骤：全局冻结、仓库收紧、仓库级巡检暂停、某条主干工作流暂停（另一条成功）、未关闭的降级 issue；分别在开工前与进行中命中；暂停的工作流之后在后继提交上成功
- 预期：开工前命中不认领、不开 issue、不记暂停，候选留队重查；进行中命中在下一步前暂停；另一条工作流的成功不解除暂停；同一工作流后继成功后解除
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_watch.ReleaseGuardTest -v`
- 证据：运行输出

### R11: 失败、超时、中断与收尾
- 步骤：任一步失败；单步超过 30 分钟；等用户超过 24 小时；暂停后所有者回复 `继续发版`；`install-local`／`verify-install`／`record` 中途中断后用户声明从哪步继续；写完 `lastReleasedSha` 后、关 issue 前中断；issue 已被关闭时续跑收尾
- 预期：失败与超时都进 `paused` 并通知用户和会话；watch 读到继续指令后续跑；本机三步中断不自动重跑、按用户声明继续；`finishing` 逐项对账、不重做不漏做，不依赖 issue 打开
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_engine.RecoveryTest tests.test_watch.ReleaseResumeTest -v`
- 证据：运行输出

### R12: 发版前汇报与发版后问题通知
- 步骤：自动发版开始；安装后验证失败；发版后主干 CI 变红（目标是已发版本后继、存在未 `ready` 的版本）；`report-failure --stage release --version`
- 预期：开始前账本有「将要做什么」并通知范围内 PR 的会话，汇报含修复次数；三种问题都通知用户并附版本、tag、Release、运行证据；主干红只归到 `ready` 的、是祖先的最新版本；不撤回、不删 tag 和 Release
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_engine.ReportTest tests.test_watch.ReleaseNoticeTest -v`
- 证据：运行输出

### R13: 人批交接
- 步骤：`--tag vX.Y.Z` 交接：远端 tag 存在且指向主干上的提交；缺 `install_local` 等入口
- 预期：跳过建／推 tag，发清单等所有者确认后从 `await-release` 继续，交接后按 `auto-confirm` 跑；缺入口的步骤标 `needsUser`
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_engine.HandoffTest -v`
- 证据：运行输出

### R14: 沙盒端到端：自动发版
- 步骤：沙盒按交接清单准备好（`release_publish` 等入口、`release-tag.yml`、tag 规则集、上限 v2）；一个需求确认时答了「做完自动发版：是」的模块合进主干、主干 CI 绿
- 预期：watch 入队并启动发版；不停下等确认，清单写进汇报；hub App 推 tag、`release-tag.yml` 建出 Release；装到本机并验证；`restart-codex` 这类步骤找用户；`lastReleasedSha` 推进
- 执行方式：manual
- 证据：用户原话（附 issue、tag、Release 链接）

### R15: 沙盒端到端：停下确认的发版
- 步骤：沙盒上一个没有提前授权的模块合进主干、主干 CI 绿；用户在发版跟踪 issue 回复 `确认发版 <指纹>`
- 预期：hub 停下列出完整清单（含要用户动手的步骤）；确认一次后自动走完清单里没标「要你动手」的步骤
- 执行方式：manual
- 证据：用户原话（附 issue 链接）

## 成功标准对照

| 成功标准 | 用例 |
|---|---|
| S1 | R14 |
| S2 | R15, R4 |
| S3 | R2 |
| S4 | R2, R3 |
| S5 | R2 |
| S6 | R1, R2 |
| S7 | R7 |
| S8 | R4 |
| S9 | R6, R7 |
| S10 | R8 |
| S11 | R9 |
| S12 | R10 |
| S13 | R11 |
| S14 | R12 |
| S15 | R5 |
| S16 | R2, R13 |

## 受影响的老功能

### R16: 旧入口、旧模板与站点发布不变
- 步骤：用旧入口 `--version`／`--resume` 跑名单外插件仓库与站点模板的发版；续跑一个起始块没有 `target` 的旧 issue
- 预期：行为与本模块之前一致（`plugin.yml` 不变、档位只读项目级、`--confirm` 照旧）；名单内仓库用旧入口新发版被拒
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_engine tests.test_pipeline tests.test_limits tests.test_release_checkout -v`
- 证据：运行输出

### R17: 原有编排、watch 与状态测试仍通过
- 步骤：跑原有的编排、watch、状态、分类测试
- 预期：除按本模块有意改变的断言（已在同一 PR 里改并说明，例如 `watch --resume` 清巡检暂停）外全部通过
- 执行方式：command
- 命令：`PYTHONPATH=tests python3 -B -m unittest tests.test_orchestrator tests.test_watch tests.test_state tests.test_classify -v`
- 证据：运行输出

### R18: 全量检查两遍
- 步骤：在仓库根目录跑全量检查，默认 Python 与 macOS 自带 3.9 各一遍
- 预期：两遍都退出 0
- 执行方式：script
- 命令：`scripts/validate.sh && PYTHON=/usr/bin/python3 scripts/validate.sh`
- 证据：运行输出

### R19: 逐仓交接验收
- 步骤：交接清单落地后，五个插件仓库各用 hub 的 App 推一个预发布版本号的 `v*` tag；用 hub 令牌读各仓 tag 规则集与绕过名单；用旧版 spec-guard（`/map --json` 缺 `releasePreauth`）跑一次判定；GraphQL 读一条评论的 `lastEditedAt`
- 预期：每个仓库的 `release-tag.yml` 都触发并建出对应 Release；规则集读得到、绕过名单只有 App 与用户本人；缺字段时按无授权停下；GraphQL 字段读得到
- 执行方式：manual
- 证据：用户原话（附各仓 Release 链接）
