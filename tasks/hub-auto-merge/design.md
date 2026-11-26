# 技术方案：hub-auto-merge

> 依据：`spec/hub-auto-merge.md`（已批）；总账 `research/2026-11-26-自动驾驶目标/` 的总体技术方案（已批）第 4.2～4.8、7、9 节、模块 1 批准稿（F3 v1.8）、`模块2-沙盒实测.md`。本模块是重要需求，本方案交第二审查者审一轮后由用户批。

## 现状调研

查了：`orchestrator/cli.py`、`orchestrator/flow.py`、`verify/core.py`、`risk/tiers.py`、`risk/templates.json`、`admission/decide.py`、`admission/cli.py`、`.github/workflows/degrade.yml`、`degrade/run.py`、`scripts/validate.sh`、`tests/` 下编排相关测试、agent-relay 的 `interface.json`。没查：pipeline 发布引擎（模块 3）、audit 合规监测（不受影响）、各插件仓库的 caller。

- **run 的顺序**（`flow.run`，`flow.py:676`）：机器人身份（:687）→ PR 打开、非草稿、非 fork（:703-706）→ 作者守卫，只放行机器人和仓库所有者（:707-709）→ 有降级 issue 先 `narrow_pending` 再停（:711-718）→ 非 spec-guard 项目停（:721-725）→ 所有模块合并档位都不是 auto 就停（:740-747）→ 风险预判 `evaluate`，不过就停（:751-754）→ 仓库检查 `run_check`、写检查证据（:756-763）→ pipeline 合并前步骤（:764-782）→ 新流程 `verify_module`（:797）／老流程看档位和最终验收（:802-812）→ 证据评论（:819-834）→ 准入 `evaluate`（:836-838）→ `PUT .../merge` 带 sha（:844）。计划内的 `Stop` 在 PR 上评论「停在…」（:882-886）；机器人身份缺失和 dry-run 不评论，`ApiError` 只在 `cli.py:942` 打印后退出 2；`cli.py:953` 只把 `merged`、`dry-run` 当成功。入口 `cli.py:881`，`cli.py:934` 传 `delegate=None`。
  - 更正：需求文档 U2、Q3 引用的 `cli.py:754`、`cli.py:705-709` 实际是 `flow.py:754`、`flow.py:705-709`。
- **老流程**：`flow.py:806-807` 在合并档位不是 auto 时先停，到不了最终验收（:808-812）。
- **组织仓库**：`flow.py:580` 在组织仓库遇到人工组就停；七个仓库的所有者 `haigeerlab` 是个人账号（`owner.type=User`，已核），不受影响。
- **跟进 issue 恢复**：`cli.py:835-851` 的 `followups` 要读当前 head 的 v2 证据和哈希相符的本机验证报告。
- **合并前验证**：`verify/core.py:6-16` 把 `manual` 和 `ai-operate` 都归人工组，其余按与基础分支的 sha256 分 auto／confirm；人工组读跟踪 issue `autopilot:verify` 里的原话（`flow.py:483-520`、:591-598）。
- **标签**：`autopilot:authored`、`autopilot:auto-merged`（`flow.py:31-32`）只在合并成功后一起打（:849）。现在的主干变红判定（`degrade/run.py:133-143`）靠 authored 找 PR，所以合并前不打会让自动合并的 PR 漏判。
- **合并后**：读回确认（:845-847）→ 标签和「已合并」评论（:849-854）→ 跟进 issue（:855-879）。
- **风险分级**：模板在 `risk/templates.json`（`tiers.py:29、34`）；common T4 为 `.github/**`、`LICENSE`、`.agent/**`、`audit/baselines.json`；spec/tasks 内容改动 T4、只勾 todo 或只改批准标记 T1、编排核对过的产物 T2（`tiers.py:108-131`）；没匹配的文件默认 T3（:80）；净删断言、压制注释净增加升 T4（:204-205、:216-217）；只有 T0～T2 能自动合并（:222）。仓库配置从基础分支读 `.github/autopr/risk-tiers.{yml,json}`（`flow.py:258-265`）。hub 自己没有这个目录。
- **降级**：`degrade.yml` 调 `degrade/run.py`：主 CI 失败且失败提交属于 authored PR 就开 L2 降级 issue（`run.py:133-143`），没有分类；撤回、回归标签、冻结标签各有一条（:119-131、:144-157、:34），非管理员关闭会被重开（:202）；去重是先读快照（:208）再建 issue（:217），workflow 并发组（`degrade.yml:34`）只能串行化 workflow 自己；收窄在 `flow.narrow_pending`（`flow.py:135-167`）里经 `cli.py:604-646` 开 T4 收窄 PR。
- **本机状态**：`~/.autopilot`（`cli.py:37`）；`config.json` 只读 `checkouts`（`cli.py:700-707`）；证据、验证报告原子写（`cli.py:474-487`、:648-656）；事件追加写 `ledger.jsonl`（`cli.py:733-746`）。没有文件锁、没有 `watch-state.json`、没有 launchd。
- **delegate**：只有 `review_acceptance`（`cli.py:524-532`）用 `delegate-exec.sh review`，只在 batch 里；没有 `delegate run`。
- **App 令牌**：`cli.py:44-45` 的 WRITE／READ 只有 contents、pull_requests、issues。
- **通知会话**：agent-relay 提供脚本发送接口 `~/.agent-relay/bin/agent-relay send --from <脚本身份> --to <会话>`（其 `interface.json`）。
- **沙盒实测**（总账 `模块2-沙盒实测.md`）：App 打开的自动合并合并后主干 CI 会跑；检查已全绿时打开自动合并被拒（`unstable status`）；推送后马上调用会报 head 对不上，约 4 秒后成功。

## 影响面

会改到的已有文件：

- `orchestrator/flow.py`：run 的停点改为「算出路线、照样验证」（含老流程 :806-807）；写 `autopilot/verified`（成功和失败都写）；路线判定；先打 authored；打开自动合并、受控的直接合并回退；ai-operate 用例走 delegate；修复计数与「改了裁判」检查；run 内的 `ApiError` 落成暂停记录。
- `orchestrator/cli.py`：令牌权限分组（run／watch／现有 batch、followups 各自一组）；`run` 加 `--notify`、`--author-host`；新增 `watch`、`report-failure` 子命令；读 `mergeMode`、`watchMainRed`；结果状态与退出码（:953）；`followups`（:835-851）供 watch 复用；LocalIO 新增 GraphQL、规则读取、通知、delegate run、临时克隆。
- `verify/core.py`：分组加 `ai` 组；`case_result`（:51）读 AI 执行记录。
- `risk/tiers.py`、`risk/templates.json`：按 R4 改 T4 清单和默认档；保护性升档改为 `human_only`。
- `admission/decide.py`：风险项同时看 `human_only`。
- `degrade/run.py`：主 CI 失败分支加开关（仓库开了 `watchMainRed` 就不处理）；撤回、回归、冻结标签、重开逻辑保留。`.github/workflows/degrade.yml` 传入开关。
- `tests/test_orchestrator.py`、`tests/test_verify.py`、`tests/test_risk.py`、`tests/test_admission.py`、`tests/test_degrade.py`、`tests/test_followups.py`：按改动补测试。
- `README.md`、`README.en.md`、`CHANGELOG.md`：现状一节、安装说明与变更记录。

新增文件：

- `orchestrator/watch.py`：巡检主体。
- `orchestrator/classify.py`：CI 失败分类。
- `orchestrator/state.py`：`watch-state.json` 读写、文件锁、意图记录、幂等记录、通知队列。
- `.github/autopr/risk-tiers.json`：hub 自己的风险类别（本身 T4）。
- `examples/launchd/com.haigeerlab.autopilot-watch.plist`：launchd 模板（不自动安装）。
- `tests/test_watch.py`、`tests/test_classify.py`、`tests/test_state.py`。

## 实现方案

### 1. 风险分级按 R4（Q1）

- `risk/templates.json` common 层的 T4 改为 R4 四类：`.github/workflows/**`；发布配置和版本号文件（`.claude-plugin/**`、`.codex-plugin/**`、`package.json`、`pyproject.toml`、`RELEASING.md`）；权限令牌密钥配置（`.github/autopr/**`、`**/*.pem`、`.env*`）；hub 自己的接管配置（`.agent/config.json`、`audit/baselines.json`、`release-limits.json`、`bot-identity.json`、`standard-target.json`）。`LICENSE`、`.agent/state.json`、`.github/` 下非 workflow 的文件不再判 T4。
- 类别层（plugin、frontend、content、contract）的规则保留；只把各类别「没匹配上」的默认档从 T3 降为 T2，contract 类别默认档保持 T4（它本身就是对外契约）。common 与类别规则按现有取高（`tiers.py:87`、:138）。完整清单在实现时与 `tests/test_risk.py` 一起列全，交 Plan 时附上。
- `spec/**`、`tasks/**`：只勾 todo、只改批准标记仍给 T1；其余内容改动不再判 T4，走普通规则（T2）。`spec/CAPABILITY-MAP.md` 的改动给 `human_only`（新模块要用户批）。
- 保护性升档（净删断言、压制注释净增加、补丁读不到）不再改 tier，改为 `human_only: true` 和 `human_reasons`；`auto_merge_eligible = rank(tier) <= 2 and not human_only`；`admission/decide.py` 风险项同时看两项。
- 新建 hub 自己的 `.github/autopr/risk-tiers.json`，放 hub 的补充 T4（`orchestrator/token.py` 这类令牌代码）。

### 2. run 的路线、`autopilot/verified` 与合并（U1、U2、Q2）

**停点改动**：「所有模块合并档位都不是 auto」（:740-747）、风险预判（:751-754）、`verify_module` 开头的档位检查（:534-535）、老流程的档位检查（:806-807）不再停下，改成记下 `route=manual` 与原因，继续跑检查和验证。降级 issue、非 spec-guard、身份和 PR 状态检查照旧停。

**验证状态**：验证结束后用 App 令牌 `POST /repos/{r}/statuses/{headOid}`，`context=autopilot/verified`：
- 通过写 `success`，`description` 带 patch id 前 12 位与验证时的基础分支提交前 12 位，`target_url` 指向证据评论；
- 不通过写 `failure`（覆盖同一提交上此前的 success），并关闭该 PR 已排队的自动合并（如有）。

**路线判定**（依次检查，先命中的决定；结果和全部原因写进证据评论和 `run` 输出）：
1. `mergeMode=direct` → `direct` 路线：完全走现有 :836-854 的准入与直接合并（回滚第 3 步），dry-run 输出 `route=direct`。
2. `watch-state` 的 `frozen`，或本仓库 `repos[...].narrowed` → `manual`。
3. 以下全部满足 → `automerge`，否则 `manual`：新流程模块；模块合并档位 `auto`；风险 `auto_merge_eligible`；准入通过；主干规则要求 `autopilot/verified` 且**来源限定为 hub App**（读 `GET /repos/{r}/rules/branches/{base}` 的 `required_status_checks`，要求该 context 的 `integration_id` 等于 App ID；旧版保护读 `required_status_checks.checks[].app_id`）；规则要求分支与主干同步（rulesets 的 `strict_required_status_checks_policy` 或旧版 `strict`）；watch 心跳在 15 分钟内。

**为什么要求「分支与主干同步」**：验证依赖基础分支的内容（`flow.py:537`）。打开自动合并后主干若前进，非同步规则下 GitHub 仍会合并，合进去的就不是验证过的组合。要求同步后，主干前进会让 PR 落后、不能合并；更新分支会产生新提交，新提交没有 `autopilot/verified`，必须重新 `run`（patch id 缓存让重跑便宜）。这一项进 B 批清单。

**automerge 路线**：
1. 先在 `watch-state` 写意图 `prs[...] = automerge-requested`（`validatedHeadOid`、`validatedBaseOid`、`notify`、时间），再打 `autopilot:authored`。
2. GraphQL `enablePullRequestAutoMerge(expectedHeadOid=validatedHeadOid)`，合并方式沿用 `flow.py:668-673`。
3. 成功 → 状态改 `automerge-on`，`run` 结果 `automerge-on`。
4. 报 head 对不上 → 间隔 2 秒最多重试 4 次；之后读 PR head：仍不是 `validatedHeadOid` → 暂停（原因「head 被改了」），通知会话重新 `run`；已对上 → 回到第 2 步再试一次，仍失败按第 5 步。
5. 其他拒绝 → 读 PR head、主干规则（同上来源和同步要求）、该提交上必需检查的结论（check-runs 和 statuses，按 App ID 核来源）：head 等于 `validatedHeadOid`、必需检查全部成功、PR 不落后主干，才 `PUT .../merge`（带 sha），然后同步做合并后收尾（:845-879，与现在相同），结果 `merged`；否则暂停并通知。

**manual 路线**（含 T4、老流程、Dependabot）：写完状态后在 PR 上评论「由你合并」及原因，`notify` 会话收到同一句，结果 `handed-to-user`。不打开自动合并。

**结果与退出码**（`cli.py:953`）：`merged`、`automerge-on`、`handed-to-user`、`dry-run` 退出 0；停下、暂停退出 1；`ApiError` 等异常先写暂停记录（`run-failed`）和通知，再退出 2。

### 3. 老流程与 Dependabot（Q3）

- 老流程：:806-807 不再先停；照 :808-812 读最终验收，通过后写 `autopilot/verified`，路线固定 `manual`。
- 作者守卫（`flow.py:707-709`）对 `dependabot[bot]` 放行，只进「依赖更新」路线：仓库检查、风险分级，通过写状态，路线固定 `manual`；不读模块、不验证、不开自动合并。

### 4. ai-operate 交 delegate（U3）

- `verify/core.py` 分组加 `ai`：执行方式 `ai-operate` 的用例归 `ai` 组。
- 执行：每条用例 `git clone --no-hardlinks` 一次性克隆到临时目录（不共享 `.git`），调 `delegate-exec.sh run --backend <非作者一方> --repo <克隆> --write -- <用例步骤、预期、证据要求>`，要求结尾一行 JSON `{"case","result":"pass|fail","evidence"}`；跑完删除克隆。
- 写进报告：结果写成执行记录 `{"executor":"delegate:<后端>","result","evidence","exit"}`，`case_result`（`verify/core.py:51`）按执行记录判定，报告里列出执行者和证据。
- 非作者一方：`run --author-host claude-code|codex` 由会话传入，选另一家；没传或另一家不可用 → 该用例转人工组。
- 不可用（找不到 delegate、版本低于 0.13、后端不可用、输出解析不到）→ 转人工组，跟踪 issue 注明「AI 执行不可用：<原因>」。七个仓库都是个人账号所有，人工组可以开跟踪 issue（`flow.py:580` 只拦组织仓库）。
- **会动本机全局状态的用例**（例如装插件再试用）：一次性克隆隔离不了 `~/.claude`、`~/.codex`。第一版这类用例标 `manual`，交用户测。这偏离了总体方案 4.3b「在一次性环境里执行」，用户已同意（2026-11-27 在 ecosystem-lead 会话原话「三件都按推荐」，经 #23378 转来）。

### 5. watch（U4～U7、4.4、4.6）

- 新子命令 `cli.py watch [--once] [--freeze|--unfreeze] [--resume <repo>[#pr]]`；launchd 每 5 分钟跑一次 `--once`。另有 `cli.py report-failure --repo --pr --stage <open-pr|...> --detail` 供会话上报 hub 看不到的失败（例如开 PR 失败），落成暂停记录并通知用户。
- `orchestrator/state.py`：`~/.autopilot/watch-state.json`，`fcntl.flock` 独占锁（拿不到直接退出）；原子写。结构：
  - `heartbeat`、`frozen`；
  - `prs["owner/repo#n"]`：`state`（automerge-requested／automerge-on／merged／post-merge-done／paused／handed-to-user）、`validatedHeadOid`、`validatedBaseOid`、`since`、`notify`、`pause`（原因、时间、恢复条件）；
  - `runs["owner/repo:<run_id>:<attempt>"]`：`event`（pull_request／push）、`headSha`、`state`（pending／green／red-classified／rerun／paused）、`classification`、`reruns`、关联 PR；
  - `repos["owner/repo"]`：`narrowed`（原因、时间、来源运行）、`watchMainRed`（从 config 读）、`cursor`（已处理到的最新运行 id）；
  - `repairs["owner/repo#n"][<原因键>]`：已计数的修复尝试（提交号列表）；
  - `done`：幂等键集合（`<repo>:<pr 或 run_id:attempt>:<head>:<动作>`）；
  - `outbox`：待发通知（目标、正文、幂等键）。
- **先记后做**：每个副作用先写意图和幂等键，做完写 `done`；崩溃后下一轮按意图对账远端（例如意图是打开自动合并，就读 PR 的 `autoMergeRequest` 判断做没做成），不重做已做的。通知一律先进 `outbox`，第 4 步统一发。
- 每轮：
  1. **冻结**：`frozen` 时只做关闭类动作：列出各仓库 `autoMergeRequest` 非空的 PR，逐个 `disablePullRequestAutoMerge`，写事件。`run` 在冻结时路线固定 `manual`（第 2 节）。
  2. **PR**：对每个 `automerge-requested`／`automerge-on` 的 PR：
     - head ≠ `validatedHeadOid`，或该 head 上 `autopilot/verified` 不是 success → 关自动合并、暂停（恢复条件：重新 `run` 通过）、通知会话；
     - 已合并 → 打 `autopilot:auto-merged`、开跟进 issue（复用 `followups` 的路径：读 `validatedHeadOid` 的 v2 证据和本机报告，`cli.py:835-851`）、写事件、`post-merge-done`；
     - 打开超过 60 分钟未合并 → 关自动合并、暂停、通知用户；
     - PR 上的 CI 运行（按 `run_id:attempt` 增量）失败 → 分类（第 6 节）；单次运行超过 60 分钟 → 暂停、通知。
  3. **主干**：只对 `watchMainRed=true` 的仓库（见第 8 节）。从 `cursor` 起逐条处理主干 push 运行（不只看最新一条）：
     - 绿 → 记 `green` 和关联 PR、`validatedHeadOid`、证据评论地址（交模块 3）；
     - 红 → 分类；`test`：按顺序 ①写 `repos.narrowed`（`run` 从此路线固定 `manual`）②关掉该仓库所有排队的自动合并 ③开降级 issue（复用 `degrade/run.py` 的开 issue 与去重函数，reason `main-red`）④调 `narrow_pending` 开 spec-guard 收窄 PR ⑤通知用户。收窄 PR 要用户合并才在 spec-guard 生效，但 ①已让 hub 立即停止打开自动合并；
     - 单次运行超过 60 分钟 → 暂停、通知。
  4. **发通知**：用户走 macOS 通知（`osascript display notification`）加 PR／issue 评论；会话走 `~/.agent-relay/bin/agent-relay send --from autopilot-watch --to <notify>`；成功才出队，失败留到下一轮。
  5. 写 `heartbeat` 和各仓库 `cursor`。
- 解除：PR 级暂停在重新 `run` 通过或 `--resume <repo>#<pr>` 后解除；仓库级 `narrowed` 只由 `--resume <repo>` 解除（用户说恢复后由会话执行）。
- 「将要做什么」：每串动作开始前写一条事件到 `ledger.jsonl`，有 `notify` 时进 `outbox`。

### 6. CI 失败分类（`classify.py`，U4、4.3）

- 输入：`GET /actions/runs/{id}` 的结论和 `GET /actions/runs/{id}/jobs` 的 job、step 名称、顺序和结论。
- 规则（按顺序）：
  - run 结论为 `startup_failure`、job 没有任何 step、或报错为 workflow 解析失败、找不到 action → `config`；
  - 失败 step 排在该 job 第一个「测试类 step」之前（测试类按名称匹配 test、pytest、unittest、validate、check，以及 job 里调用测试脚本的 step）→ `env`；
  - 已重跑过且重跑后变绿 → `env`（事后归类，只记录）；
  - 其余，以及判不准 → `test`。
- `env`：`POST /actions/runs/{id}/rerun-failed-jobs`，同一运行最多两次；两次后仍红 → 暂停、通知用户（总体方案 4.3「三种都有终点」）。
- `config`：PR 上或主干上都暂停，通知用户和会话；会话准备 T4 修改 PR，由用户合；不收紧档位。
- `test`：PR 上 → 通知会话分析（代码问题按第 7 节修；判为用例问题停下交用户）；主干上 → 第 5 节第 3 步。

### 7. 修复上限与「不许改裁判」（4.7）

- 原因键：`check:<检查名>`、`case:<R 编号>`、`review:<意见 id>`。三种来源各是各的原因，但共用同一套 `repairs` 记录和「每个原因最多修两次」的上限。
- 计一次修复：某原因在提交 A 上失败后，PR 出现新的 head B（即会话改了代码），B 上同一项重新跑过（检查跑完、用例重新执行、或审查重审）。B 上又失败 → 记一次（把 B 记进该原因的列表）；B 上通过 → 不再计。`hub run` 在验证时、watch 在 PR CI 结束时各自按这个规则记；同一提交不重复记。
- 第三次修复的结果出来前停下：同一原因已记两次时，`run`／watch 进入暂停，通知用户，附「出了什么事、试过的两个提交、可选做法和代价」。
- 「改了裁判」：`hub run` 计算 PR 相对需求开工基线（spec-guard `scope_base`）的改动；改了基线已有的测试文件或 `tasks/<id>/regression.md` → 暂停交用户。测试文件的判定复用 `risk/templates.json` 里现有的测试路径模式（含 `tests/**`、`test/**`、`**/test-*.sh`、`*.spec.*` 等），不另立一份。现有「不许净删断言」检查保留。

### 8. 降级 workflow 与 watch 并存（U5）

- 新增仓库级开关 `~/.autopilot/config.json` 的 `repos.<repo>.watchMainRed`（默认 false）。B 批里某仓库 caller 升钉到含本模块的 hub 提交、并装好 watch 后，才把它设为 true。
- `degrade.yml` 新增输入 `skip_main_red`；各仓库 caller 升钉时同时设为 true。`degrade/run.py` 在该输入为 true 时跳过主 CI 失败分支（:133-143）；撤回、回归、冻结标签、非管理员关闭重开都保留。
- 这样任一时刻每个仓库只有一方处理主干变红：升钉前是旧 workflow（不分类，和现在一样），升钉且打开开关后是 watch。不靠 reason+ref 去重来解决并发。

### 9. 令牌权限

- `run`：contents、pull_requests、issues 写，statuses 写（新增）。
- `watch`：contents 读，pull_requests、issues 写，statuses 读，actions 写（重跑）。
- `batch`、`followups`：不变（`cli.py:754`、:822 现有 WRITE）。
- 路线判定的「部署前提」里，App 权限不足会在调用时报错，按暂停处理；B 批清单写明需要的 App 权限。

### 10. 交给 B 批与模块 3

- `examples/launchd/com.haigeerlab.autopilot-watch.plist`；README 写安装、卸载命令（`launchctl bootstrap/bootout gui/$UID`）。
- README「打开自动路之前」逐仓库清单：App 装好，权限含 statuses 写、actions 读写、checks 只读；watch 已装、心跳新鲜；caller 升钉到含本模块的提交且 `skip_main_red: true`；`config.json` 设 `watchMainRed: true`；主干规则加必需检查 `autopilot/verified`（来源限定 App ID）并要求分支与主干同步；沙盒先跑通。
- 模块 3：`watch-state.json` 的 `runs` 里主干绿的运行记目标提交、关联 PR、`validatedHeadOid`、证据评论地址。

## 接口和数据改动

- 提交状态 `autopilot/verified`：通过写 success，不通过写 failure。
- 标签：`autopilot:authored` 改为打开自动合并前打；`autopilot:auto-merged` 由 watch 在合并后打（direct 路线仍在合并后同步打）。
- CLI：`run` 加 `--notify <会话名>`、`--author-host`；结果新增 `automerge-on`、`handed-to-user`，退出码见第 2 节；新增 `watch`、`report-failure`。
- 本机：`~/.autopilot/config.json` 新增 `mergeMode`（默认 `automerge`）、`repos.<repo>.watchMainRed`；新增 `~/.autopilot/watch-state.json`、`~/.autopilot/watch.lock`。
- `degrade.yml` 新增输入 `skip_main_red`（默认 false，旧 caller 不受影响）。
- 风险分级结果新增 `human_only`、`human_reasons`；`auto_merge_eligible` 语义收紧。
- 证据评论新增 `route`、`routeReasons`、`validatedHeadOid`、`validatedBaseOid`。

## 风险与回滚

| 风险 | 对策 |
|---|---|
| 风险分级放宽后坏代码被自动合并 | 合并前验证和 `CI required` 仍是必需；保护性升档保留为 `human_only`；沙盒先跑通 |
| 提交状态被别人写 | 必需检查来源限定 App ID，路线判定和直接合并回退都核来源 |
| 主干前进后合进未验证的组合 | 要求分支与主干同步；落后的 PR 更新后是新提交、没有状态 |
| 重新验证失败但旧的 success 还在 | 失败写 failure 覆盖，并关掉排队的自动合并 |
| watch 没装或停了 | 路线判定要求心跳新鲜，否则走人工路；合并本身在 GitHub 完成 |
| 打开自动合并后又推提交 | 状态绑定提交（第一道）；watch 核对 head 关闭（第二道） |
| 主干变红漏判、重复处理 | 打开自动合并前就打 authored；`watchMainRed` 开关保证同一时刻只有一方处理 |
| 收紧要等收窄 PR 合并才生效 | 先写 `repos.narrowed`，hub 立即停止打开自动合并、关掉队列 |
| 崩溃导致副作用丢失或重复 | 先记意图后做事，下一轮按远端状态对账；幂等键含运行编号和重跑次数 |
| AI 用例结果不稳 | 解析不到即转人工；动全局状态的用例第一版不自动执行 |
| 分类判错 | 只有失败在测试之前、或重跑变绿才算 env，其余按 test |

回滚按总体方案第 9 节：`watch --freeze` → 冻结后第一轮关掉所有排队的自动合并 → `mergeMode: direct`（`run --dry-run` 显示 `route=direct`）→ 卸 launchd → spec-guard `takeover` 改回 `none`。

## 验收方式（对应需求成功标准）

| 标准 | 怎么验 |
|---|---|
| S1 | 沙盒新流程 PR（档位 auto、非 T4、主干规则已加状态且要求同步）：证据评论 `route=automerge`；时间线上 authored 标签早于自动合并；合并后主干树等于 `validatedHeadOid` 的树；run 进程在 `automerge-on` 后已退出 |
| S2 | 去掉主干规则里的状态要求再跑：`route=manual`，原因含「主干未要求 autopilot/verified」 |
| S3 | 改 `.github/workflows/` 的 PR：状态 success、没有 `autoMergeRequest`、收到「由你合并」 |
| S4 | 故意让一条回归用例失败：状态 failure、没有自动合并、没有合并 |
| S5 | 打开自动合并后推新提交：GitHub 不合；下一轮 watch 关掉自动合并并给会话发消息 |
| S6 | 检查已全绿时 run：读回核对后直接合并；人为改 head 后再试：不合并 |
| S7 | 含 `ai-operate` 用例的模块：报告里该用例执行者为 delegate；卸掉 delegate 再跑：转人工 |
| S8 | 分别造三种 CI 失败（setup 步骤失败、workflow 语法错、测试失败）：重跑／暂停交用户／通知会话 |
| S9 | 同一回归用例连续两次修复仍失败：第三次前暂停；改已有测试：立即暂停 |
| S10 | 主干测试失败：`narrowed` 已写、队列清空、降级 issue 和收窄 PR 已开、用户收到通知；无撤回、无发版；重新 run 通过不恢复，`--resume` 才恢复 |
| S11 | 逐个模拟 U7 的失败（含 `report-failure`、run 内 `ApiError`）：都有暂停记录和通知 |
| S12 | 回滚第 1～3 步逐步验证 |
| S13 | watch 停 30 分钟期间主干有两次运行、PR 合并一次：恢复后两次运行都处理、跟进 issue 只开一次、通知不重复 |

## 方案取舍

- **路线判定放在 run 里**：GitHub 只管「能不能合」，不知道档位、T4、watch 是否在线；hub 必须先判定。
- **验证失败写 failure**：同一提交可能先通过后失败（重跑），不写 failure 会留下旧的 success。
- **要求分支与主干同步，而不是让 watch 比对基础分支**：watch 每 5 分钟一次，窗口内可能已经合了；GitHub 侧的同步要求没有窗口。代价是主干一动，排队的 PR 都要更新并重跑验证（patch id 缓存让重跑主要是跑检查）。
- **ai-operate 用 delegate run**：总体方案 4.3b 已定。
- **watch 一个进程管所有仓库**：总需求 Q1 已定；共用锁和状态文件。
- **主干变红由 watch 分类，旧 workflow 按开关退出**：分类要重跑和读 job 详情，放在本机更好控制；用开关而不是去重，避免两边同时处理。
- **不做 hub 的推送受控入口**：Q6 已定交 A10。

## 已定的偏离与外部前提

- 动本机全局状态的 `ai-operate` 用例第一版标 `manual`，偏离总体方案 4.3b，用户已同意（见第 4 节）。
- B 批清单已由 ecosystem-lead 记入模块 1（总账 8c32ddb）：主干「要求分支与主干同步」、`skip_main_red`、`watchMainRed`、App 的 Checks 只读、Actions 读写。
- Q6：Codex 本机规则已补放行 `git switch -c`、`git add`、`git commit`、`git push -u origin`、`gh pr create`（A10）；用户自己的 Codex 会话复测由用户安排，不在本模块。

## 审查记录

第一轮：Codex 只读审查（delegate design profile，2026-11-27），17 条意见（13 条阻断、4 条建议），全部采纳：必需检查核来源（App ID）和同步要求；验证失败写 failure 并关队列；冻结和收紧先落状态、进入路线判定；旧 degrade 与 watch 用 `watchMainRed`／`skip_main_red` 开关错开，不靠去重；U7 每种失败都有暂停终点（含环境重跑耗尽、PR 上 CI 超时、`report-failure`、run 内 `ApiError`）；修复按「新提交上重跑仍失败」计次、测试路径复用模板；先记意图后做事、按运行编号和重跑次数做幂等；老流程不再在档位处先停、类别规则的去留写明；AI 用例写入执行记录、组织仓库停点核实不影响；`direct` 路线优先级、dry-run 输出、合并后收尾写明；退出码和 followups 证据复用写进影响面；动全局状态的 AI 用例列为待定回报；令牌按命令分组；分类按失败 step 位置、主干配置错误有终点；现状描述修正、旧降级入口保留；补验收方式表。
