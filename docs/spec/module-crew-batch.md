# module-crew 合并第一段对接

> 2026-11-22。来源：合并方案 Q9、mc-coord #22078/#22085、sg-coord #22088/#22098；用户原话「按这个做法派」。

## 职责与用法

module-crew 只在本机把模块合进集成分支。autopilot 读取它的合并第一段接口，负责推 batch 分支、开 PR、重跑检查、补最终验收和远端合并。调用方向只有 autopilot → 插件，插件不调用 autopilot。

```bash
python3 -B orchestrator/cli.py batch --repo owner/name --dry-run
python3 -B orchestrator/cli.py batch --repo owner/name
```

一次只推进一个模块。退出码：0 为已合并、没有待处理的合并或预演完成；1 为停下，正常运行会在已有 PR 上留下机器人评论；2 为参数、环境或接口问题。预演始终不写远端或本机验收，不跑检查和审查。

## 本机配置与接口

在 `~/.autopilot/config.json` 配置用户本机项目检出：

```json
{"checkouts": {"owner/name": "/本机/项目检出"}}
```

在该检出里执行 `module-crew merge first-stage --json`，可执行文件由 `AUTOPILOT_MODULE_CREW` 指定，缺省使用 PATH。命令失败、输出不是 JSON、`schemaVersion` 不是整数 1、`repo` 与 `--repo` 不同，均按环境或接口问题退出。用于 Git ref、路径与命令的模块名、提交 SHA、批准事项号也会校验。

spec-guard 的根目录由 `AUTOPILOT_SPEC_GUARD_ROOT` 指定；缺省选 `~/.claude/plugins/cache/spec-guard-marketplace/spec-guard/` 下最新的 ≥0.92.0 版本（接口 1.16）。最终验收调用该版本的 `hooks/map_view.py` 和 `hooks/review_mark.py`；现有 flow 的模块产物核对也支持指定根目录。补审使用现有 `AUTOPILOT_DELEGATE` / delegate 0.10.0+ 发现机制。

机器人令牌仅限目标仓库，正常运行使用 contents、pull_requests、issues 的 write，预演使用 read；结束时作废。推送认证通过 `GIT_CONFIG_*` 环境变量传给 Git，不进入 argv。插件命令、检查与审查不带 hub 的 GitHub 令牌。

## 单模块队列

按 `merges` 顺序取 `auto == true` 的候选，跳过：

- 合并提交已经是远端 `baseBranch` 的祖先。
- 同仓库 `autopilot/batch-<module>` 分支已有 head SHA 等于合并提交的已合并 PR。

已有打开的 batch PR 时先续跑它，不开第二个。续跑前核对机器人身份、正文从未编辑、机器块、head SHA 和基础分支；不符则停下交人核对。正文编辑时间使用 GitHub GraphQL `lastEditedAt`，评论、标签导致 REST `updated_at` 变化不会被误认为正文编辑。

没有打开的 PR 时，先读取远端基础分支提交，再用 GitHub compare 判断祖先关系。module-crew 只做本地合并，GitHub 可能还不知道合并提交；基础分支已确认存在后，compare 返回 404 表示该本地提交仍需处理。没有剩余候选时打印「没有待处理的合并」。

新候选在本机检出中执行：

```text
git push --force https://github.com/<repo> <mergeCommit>:refs/heads/autopilot/batch-<module>
```

然后由机器人开 PR，base 使用接口的 `baseBranch`，标题为「autopilot：合入模块 <module>（module-crew 合并第一段 C<approval>）」，正文列出合并提交、模块提交、module-crew 批准事项号和审查报告指纹，末尾带：

```html
<!-- autopilot-batch:v1 {"module":"x","mergeCommit":"完整 SHA","approval":123} -->
```

PR 打 `autopilot:authored` 标签。已有打开的匹配 PR 直接续跑，保留其 head。

## 检查、审查与验收

先调用现有 `flow.run`。风险、接管档位、降级、检查、准入和匹配 head 合并等规则继续生效，其他停止原因原样返回。

batch 的每次 `flow.run` 调用都显式传入 `merge_method="merge"`，必须以 merge commit 合并，不能 squash 或 rebase。module-crew 的合并是叠起来的：第 k 个模块的 `mergeCommit` 以第 k-1 个模块的 `mergeCommit` 为父；压平或变基会使原提交不再是远端基础分支的祖先，下一个模块的 PR 会重复带入上一个模块的改动，还会使本地集成分支与远端基础分支分叉。若仓库 `allow_merge_commit` 为假，flow 停在 `merge`，不调用合并接口，提示「仓库不允许 merge commit，module-crew 的合并是叠起来的，不能压平或变基，交你在仓库设置里打开 Allow merge commits」。普通 PR 不传此参数，仍按仓库设置优先 squash。

flow 返回 `merged` 后，batch 额外请求 GitHub compare `mergeCommit...baseBranch`，核对 module-crew 原始合并提交是远端基础分支的祖先。只有 `status` 为 `ahead` 或 `identical` 才返回退出码 0；其他状态或 compare 返回 404 时，保留已合并结果，在 `notes` 写明祖先核对失败并返回退出码 1。

若停在 `acceptance`，在合并提交上建立临时 detached worktree：

1. 运行 `map_view.py --json --project <worktree>`。模块的 `codexRoute.spec` 和 `.plan` 必须都为 null；非 null 说明 spec-guard 判定要人批，缺少路由信息也停下。
2. 优先复用接口的 `release.report`：文件能读到、原始字节 sha256 等于 `reportSha256`，且报告 `scope.baseline.to == mergeCommit`。
3. 不能复用时，自己跑最终验收审查：

   ```text
   delegate-exec.sh review --repo <worktree> --base <模块开工前提交> --head <mergeCommit> --spec spec/<module>.md -- <任务说明>
   ```

   模块开工前提交取 `git log --diff-filter=A --format=%H -- spec/<module>.md` 最后一行的父提交；没有可用父提交就停下。任务说明明确流程文件 `spec/CAPABILITY-MAP.md`、用法草案 `spec/usage/`、`.agent/config.json`、`.agent/state.json` 不算实现范围，并附 `~/.autopilot/evidence/<owner>__<repo>/<mergeCommit>.json` 的每条检查命令、退出码、output_sha256 和最后 10 行 tail。

   新报告存为 `~/.autopilot/reviews/<owner>__<repo>/acceptance-<module>-<mergeCommit前12位>.json`。
4. 审查没有结论则按现有 Codex 不可用规则停止，并记 `ledger.jsonl`。有 blocking 则交人；没审全、没有补丁指纹、报告绑定的提交不符也停下。
5. 调用：

   ```text
   review_mark.py --project <worktree> --module <module> --target acceptance --state codex-approved --review <报告> --confirm
   ```

   spec-guard 自己检查 HEAD 的本机证据、报告范围、干净工作区和 Codex 路由。拒绝时保留原因码与输出前 300 字，包括 `codex-route-takeover`、`codex-route-important`、`codex-route-pre-declared`，不尝试绕过。
6. 成功后必须从共享 git 目录读回 `sha == mergeCommit` 的记录，再用现有 `accepted_review` 验证记录及报告。然后再调用一次 `flow.run`，检查会重跑，验收审查可复用。

成功、停止和异常退出均在 finally 中执行 `git worktree remove --force` 清理临时 worktree；清理失败会明确报错。

## 验收记录与额外证据

`LocalIO.find_modules` 和 `LocalIO.acceptance` 优先读 `$(git rev-parse --git-common-dir)/spec-guard/acceptance/`，再读旧的 `$(git rev-parse --git-path spec-guard/acceptance)`。同名模块记录只取新位置，去重；新记录无效或绑定其他提交时，不会恢复旧批准。

batch 在调用 flow 前传入 module-crew 检查信息。flow 再核对当前 PR 是未编辑的机器人 batch PR，且来源与提交匹配，才在证据评论可读部分加入：

```text
module-crew 合并队列检查：<command>，<result>，耗时 <seconds> 秒（额外参考，不替代本机重跑）
```

这行不进入硬卡 checks 机器数据；autopilot 的本机检查仍必须独立通过。

## 验证

`tests/test_batch.py` 使用假 GitHub、假子进程与临时文件覆盖接口校验、候选筛选、已有 PR 续跑、认证参数、PR 来源块和标签、验收闭环、路由交人、报告复用与补审参数、拒绝原因、清理异常路径、只读预演和验收目录优先级；另用真实 flow 接假 I/O 验证检查 → 验收 → 再检查 → 合并。

合并回归覆盖 batch 强制 merge commit、仓库禁用 merge commit 时停下且不调用合并接口、普通 PR 仍优先 squash，以及合并后的远端祖先核对失败时退出 1。

```bash
python3 -B -m unittest discover -s tests
/usr/bin/python3 -B -m unittest discover -s tests
```
