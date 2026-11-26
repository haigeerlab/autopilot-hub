# 技术方案：hub-auto-merge

> 依据：`spec/hub-auto-merge.md`（已批）；总账 `research/2026-11-26-自动驾驶目标/` 的总体技术方案（已批）第 4.2～4.8、7、9 节、模块 1 批准稿（F3 v1.8）、`模块2-沙盒实测.md`。本模块是重要需求，本方案交第二审查者审一轮后由用户批。

## 现状调研

查了：`orchestrator/cli.py`、`orchestrator/flow.py`、`verify/core.py`、`risk/tiers.py`、`risk/templates.json`、`admission/decide.py`、`admission/cli.py`、`.github/workflows/degrade.yml`、`degrade/run.py`、`scripts/validate.sh`、`tests/` 下编排相关测试、agent-relay 的 `interface.json`。没查：pipeline 发布引擎（模块 3）、audit 合规监测（不受影响）、各插件仓库的 caller。

- **run 的顺序**（`flow.run`，`flow.py:676`）：机器人身份（:687）→ PR 打开、非草稿、非 fork（:703-706）→ 作者守卫，只放行机器人和仓库所有者（:707-709）→ 有降级 issue 先 `narrow_pending` 再停（:711-718）→ 非 spec-guard 项目停（:721-725）→ 所有模块合并档位都不是 auto 就停（:740-747）→ 风险预判 `evaluate`，不过就停（:751-754）→ 仓库检查 `run_check`、写检查证据（:756-763）→ pipeline 合并前步骤（:764-782）→ 新流程 `verify_module`（:797）／老流程看档位和最终验收（:802-812）→ 证据评论（:819-834）→ 准入 `evaluate`（:836-838）→ `PUT .../merge` 带 sha（:844）。所有停下都在 PR 上评论「停在…」（:882-886）。入口 `cli.py:881`，`cli.py:934` 传 `delegate=None`。
  - 更正：需求文档 U2、Q3 引用的 `cli.py:754`、`cli.py:705-709` 实际是 `flow.py:754`、`flow.py:705-709`。
- **合并前验证**：`verify/core.py:6-16` 把 `manual` 和 `ai-operate` 都归人工组，其余按与基础分支的 sha256 分 auto／confirm；人工组读跟踪 issue `autopilot:verify` 里的原话（`flow.py:483-520`、:591-598）。
- **标签**：`autopilot:authored`、`autopilot:auto-merged`（`flow.py:31-32`）只在合并成功后一起打（:849）。现在的主干变红判定（`degrade/run.py:133-143`）靠 authored 找 PR，所以合并前不打会让自动合并的 PR 漏判。
- **合并后**：读回确认（:845-847）→ 标签和「已合并」评论（:849-854）→ 跟进 issue（:855-879）。
- **风险分级**：模板在 `risk/templates.json`（`tiers.py:29、34`）；common T4 为 `.github/**`、`LICENSE`、`.agent/**`、`audit/baselines.json`；spec/tasks 内容改动 T4、只勾 todo 或只改批准标记 T1、编排核对过的产物 T2（`tiers.py:108-131`）；没匹配的文件默认 T3（:80）；净删断言、压制注释净增加升 T4（:204-205、:216-217）；只有 T0～T2 能自动合并（:222）。仓库配置从基础分支读 `.github/autopr/risk-tiers.{yml,json}`（`flow.py:258-265`）。hub 自己没有这个目录。
- **降级**：`degrade.yml` 调 `degrade/run.py`：主 CI 失败且失败提交属于 authored PR 就开 L2 降级 issue（`run.py:133-143`），没有分类；撤回、回归标签各有一条（:119-131、:144-157）；收窄在 `flow.narrow_pending`（`flow.py:135-167`）里经 `cli.py:604-646` 开 T4 收窄 PR。
- **本机状态**：`~/.autopilot`（`cli.py:37`）；`config.json` 只读 `checkouts`（`cli.py:700-707`）；证据、验证报告原子写（`cli.py:474-487`、:648-656）；事件追加写 `ledger.jsonl`（`cli.py:733-746`）。没有文件锁、没有 `watch-state.json`、没有 launchd。
- **delegate**：只有 `review_acceptance`（`cli.py:524-532`）用 `delegate-exec.sh review`，只在 batch 里；没有 `delegate run`。
- **App 令牌**：`cli.py:44-45` 的 WRITE／READ 只有 contents、pull_requests、issues。
- **通知会话**：agent-relay 提供脚本发送接口 `~/.agent-relay/bin/agent-relay send --from <脚本身份> --to <会话>`（其 `interface.json`）。
- **沙盒实测**（总账 `模块2-沙盒实测.md`）：App 打开的自动合并合并后主干 CI 会跑；检查已全绿时打开自动合并被拒（`unstable status`）；推送后马上调用会报 head 对不上，约 4 秒后成功。

## 影响面

会改到的已有文件：

- `orchestrator/flow.py`：run 的停点改为「算出路线、照样验证」；写 `autopilot/verified`；先打 authored；打开自动合并与回退直接合并；ai-operate 用例走 delegate；修复计数与「改了裁判」检查。
- `orchestrator/cli.py`：令牌权限加 statuses；`run` 接受 `--notify <会话名>`；新增 `watch` 子命令；读 `mergeMode`；LocalIO 新增 GraphQL、状态写入、通知、delegate run 的实现。
- `verify/core.py`：分组加 `ai` 组。
- `risk/tiers.py`、`risk/templates.json`：按 R4 改 T4 清单和默认档；保护性升档改为 `human_only`。
- `admission/decide.py`：风险项同时看 `human_only`。
- `degrade/run.py`、`.github/workflows/degrade.yml`：去掉主 CI 失败直接开降级 issue 的分支（改由 watch 分类后开）。
- `tests/test_orchestrator.py`、`tests/test_verify.py`、`tests/test_risk.py`、`tests/test_admission.py`、`tests/test_degrade.py`：按改动补测试。
- `README.md`、`README.en.md`、`CHANGELOG.md`：现状一节与变更记录。

新增文件：

- `orchestrator/watch.py`：巡检主体。
- `orchestrator/classify.py`：CI 失败分类。
- `orchestrator/state.py`：`watch-state.json` 读写、文件锁、幂等记录、通知队列。
- `.github/autopr/risk-tiers.json`：hub 自己的风险类别（本身 T4）。
- `examples/launchd/com.haigeerlab.autopilot-watch.plist`：launchd 模板（不自动安装）。
- `tests/test_watch.py`、`tests/test_classify.py`、`tests/test_state.py`。

## 实现方案

### 1. 风险分级按 R4（Q1）

- `risk/templates.json` common 层 T4 改为四类：`.github/workflows/**`；发布配置和版本号文件（`.claude-plugin/**`、`.codex-plugin/**`、`package.json`、`pyproject.toml`、`RELEASING.md`、`.github/release*`）；权限令牌密钥配置（`.github/autopr/**`、`**/*.pem`、`**/*secret*`、`.env*`）；hub 自己的接管配置（`.agent/config.json`、`audit/baselines.json`、`release-limits.json`、`bot-identity.json`、`standard-target.json`）。`LICENSE`、`.agent/state.json` 不再判 T4。具体清单在实现时与 `tests/test_risk.py` 一起定，交审时列全。
- `spec/**`、`tasks/**` 内容改动不再判 T4：`spec_guard_tier` 改成只在只勾 todo／只改批准标记时给 T1，其余返回 None 走普通规则。`spec/CAPABILITY-MAP.md` 的「永不算批准」保留为 `human_only`（新模块要用户批，原则不变）。
- 没匹配的文件默认从 T3 改为 T2（`tiers.py:80` 与各类别默认档）。
- 保护性升档（净删断言、压制注释净增加、补丁读不到）不再改 tier，改为在结果里加 `human_only: true` 和原因；`auto_merge_eligible = rank(tier) <= 2 and not human_only`；`admission/decide.py` 的风险项同时看这两项。
- 新建 hub 自己的 `.github/autopr/risk-tiers.json`，只放 hub 的补充 T4（上面接管配置那几项若不在通用层就放这里）。

### 2. run 的路线与 `autopilot/verified`（U1、U2、Q2）

- 停点改动：「所有模块合并档位都不是 auto」（:740-747）、风险预判（:751-754）、`verify_module` 开头的档位检查（:534-535）不再停下，改成记下 `route = manual` 和原因，继续跑检查和验证。降级 issue、非 spec-guard、身份和 PR 状态检查照旧停。
- 验证全部通过后：用 App 令牌 `POST /repos/{r}/statuses/{validatedHeadOid}`，`context=autopilot/verified`、`state=success`、`description` 写 patch id 前 12 位，`target_url` 指向证据评论。验证不通过不写（不写 failure，避免和「没跑」混淆）。
- 路线判定（满足全部才 `automerge`，否则 `manual`）：新流程模块；模块合并档位 `auto`；风险 `auto_merge_eligible`；准入通过；主干规则要求 `autopilot/verified`（读 `GET /repos/{r}/rules/branches/{base}` 与旧版保护 `required_status_checks`，任一含该 context 即算）；`~/.autopilot/config.json` 的 `mergeMode` 不是 `direct`；`watch` 心跳新鲜（`watch-state.json` 的 `heartbeat` 在 15 分钟内）。缺哪条写进证据评论。
- `automerge` 路线：打 `autopilot:authored` → GraphQL `enablePullRequestAutoMerge(expectedHeadOid=validatedHeadOid)`，合并方式沿用 `flow.py:668-673` 的选择。
  - 返回 head 对不上：间隔 2 秒最多重试 4 次，再读 PR head；仍不是 `validatedHeadOid` 就停（head 被改了）。
  - 返回其他拒绝：读 PR head 和 `GET /commits/{sha}/check-runs`、`/status` 的必需检查结论；head 等于 `validatedHeadOid` 且必需检查全部成功，才走现有 `PUT .../merge`（带 sha）；否则停。
  - 成功：在 `watch-state.json` 记 PR 事项 `automerge-on`（`validatedHeadOid`、时间、`notify` 会话名）。合并后的标签、跟进 issue 由 watch 做。
- `manual` 路线（含 T4）：写完状态后在 PR 上评论「由你合并」及原因；`notify` 会话收到同一句。不打开自动合并。
- `mergeMode: direct`：保留现有的 `PUT .../merge` 流程（总体方案第 9 节回滚第 3 步），观察一周后另行删除。

### 3. 老流程与 Dependabot（Q3）

- 老流程模块：照现有 :802-812（最终验收）走，通过后写 `autopilot/verified`，路线固定 `manual`。
- 作者守卫（`flow.py:707-709`）对 `dependabot[bot]` 放行，但只进「依赖更新」路线：只跑仓库检查和风险分级，通过写状态，路线固定 `manual`；不读模块、不验证、不开自动合并。

### 4. ai-operate 交 delegate（U3）

- `verify/core.py` 分组加 `ai`：执行方式 `ai-operate` 的用例归 `ai` 组（现归人工组）。
- `flow.verify_module` 对 `ai` 组：在一次性克隆的独立副本里调 `delegate-exec.sh run --backend <非作者一方> --repo <副本> --write -- <用例的步骤、预期、证据要求>`，要求结尾输出一行 JSON `{"case":"R3","result":"pass|fail","evidence":"..."}`；解析不到或 delegate 退出非 0 算「不能执行」。
- 非作者一方：证据评论里记录作者宿主（会话调 `run` 时传 `--author-host claude-code|codex`），选另一家；拿不到就退回 `manual`。
- 不可用（找不到 delegate、版本低于 0.13、退出码表示后端不可用）：该用例转入人工组，跟踪 issue 里注明「AI 执行不可用」。
- 隔离：每条用例一份独立副本（`git worktree add --detach` 到临时目录），跑完删除；不给网络以外的额外权限，凭 delegate 自身的沙箱。会动本机全局状态的用例（例如装插件）不在第一版自动执行，标 `manual`，写进回归测试文档规范。

### 5. watch（U4～U7、4.4、4.6）

- 新子命令 `cli.py watch [--once] [--freeze|--unfreeze] [--resume <repo>[#pr]]`；launchd 每 5 分钟跑一次 `--once`。
- `orchestrator/state.py`：`~/.autopilot/watch-state.json`，`fcntl.flock` 独占锁（拿不到直接退出）；原子写；结构：
  - `heartbeat`、`frozen`；
  - `prs["owner/repo#n"]`：`state`（verified／automerge-on／merged／post-merge-done／paused）、`validatedHeadOid`、`since`、`notify`、`pause`（原因、时间、恢复条件）；
  - `mainRuns["owner/repo@sha"]`：`state`（pending／green／red-classified／paused）、`classification`、关联 PR；
  - `repos["owner/repo"]`：`narrowed`（原因、时间）；
  - `repairs["owner/repo#n:<原因键>"]`：次数；
  - `done`：幂等键集合（`repo#pr|run:<sha>:<动作>`）；
  - `outbox`：待发通知。
- 每轮：
  1. 冻结时只做「关闭」：列出各仓库 `autoMergeRequest` 非空的 PR，逐个 `disablePullRequestAutoMerge`，写事件。
  2. 对每个 `automerge-on` 的 PR：head ≠ `validatedHeadOid` → 关自动合并、暂停、通知会话；已合并 → 打 `autopilot:auto-merged`、开跟进 issue（复用 `open_followups`）、写事件、`post-merge-done`；打开超过 60 分钟未合 → 关自动合并、暂停、通知用户；PR 上有失败的 check → 分类（第 6 节）。
  3. 对每个仓库主干最新的 push CI 运行：绿 → 记 `green` 和关联 PR（交模块 3）；红 → 分类；测试失败 → 开降级 issue（复用 `degrade/run.py` 的开 issue 与去重函数，reason `main-red`）、立即调 `narrow_pending` 收窄、关掉该仓库所有排队的自动合并、记 `repos.narrowed`、通知用户；单次运行超过 60 分钟 → 暂停、通知。
  4. 发 `outbox`：用户走 macOS 通知（`osascript display notification`）加 PR／issue 评论；会话走 `agent-relay send --from autopilot-watch --to <notify>`；成功才出队。
  5. 写 `heartbeat`。
- 仓库级收紧只由 `--resume <repo>`（用户说恢复后由会话执行）解除；`hub run` 在仓库 `narrowed` 时路线固定 `manual`。
- 「将要做什么」：每串动作开始前写一条事件到 `ledger.jsonl`，有 `notify` 时同步发给会话。

### 6. CI 失败分类（`classify.py`，U4、4.3）

- 输入：`GET /actions/runs/{id}/jobs` 的 job、step 名称和结论。
- 规则：job 未能启动、workflow 解析失败、找不到 action（run 结论 `startup_failure` 或 job 无 step）→ `config`；失败 step 名称匹配 checkout／setup／install／cache／restore 一类，或重跑后变绿 → `env`；其余 → `test`。判不准 → `test`。
- `env`：`POST /actions/runs/{id}/rerun-failed-jobs`（App 需 Actions 读写，Q4），同一运行最多两次；绿了继续，不收紧。
- `config`：PR 上 → 暂停、通知用户和会话（会话准备 T4 修改 PR）；主干上 → 同 `test` 处理但原因写 `config`。
- `test`：PR 上 → 通知会话分析，计数见第 7 节；主干上 → 第 5 节第 3 步。

### 7. 修复上限与「不许改裁判」（4.7）

- 原因键：`check:<检查名>`、`case:<R 编号>`、`review:<意见 id>`；三种来源共用 `repairs` 计数。`hub run` 每次被调用时，对上一次记录的失败原因、本次又失败的加一；`watch` 对 PR CI 的 `test` 失败加一。到 2 次后第三次直接暂停并通知用户（附试过什么、可选做法）。
- 「改了裁判」：`hub run` 计算 PR 相对需求开工基线（spec-guard `scope_base`）的改动；改了基线已有的测试文件（`tests/**`、`test_*.py`、`*_test.*`、`*.test.*`）或 `tasks/<id>/regression.md` → 暂停交用户。现有「不许净删断言」规则检查保留。

### 8. 降级 workflow（U5）

- `degrade/run.py` 删除主 CI 失败直接开 issue 的分支（:133-143）；撤回、回归标签两条保留。需要新的 hub 提交号，各仓库 caller 升钉归 B 批；升钉前旧行为继续生效，与 watch 并存时由 `run.py` 的 reason+ref 去重（:208-214）避免重复 issue。

### 9. 交给 B 批与模块 3

- `examples/launchd/com.haigeerlab.autopilot-watch.plist`；README 写安装、卸载命令（`launchctl bootstrap/bootout gui/$UID`）。
- README 写「加 `autopilot/verified` 必需检查前」检查清单：App 装好且有 statuses 写、Actions 读写；watch 已装且心跳新鲜；本仓库 caller 已升到含本模块的提交；沙盒跑通。
- 模块 3：`watch-state.json` 的 `mainRuns` 记目标提交、关联 PR 号和 `validatedHeadOid`、证据评论地址。

## 接口和数据改动

- App 令牌：WRITE 加 `statuses: write`、`actions: write`（重跑用，watch 单独申请）。App 本身的权限调整（Checks 降只读、加 Actions 读写）由用户在 B 批做。
- 新提交状态 `autopilot/verified`（只写 success）。
- 标签：`autopilot:authored` 改为打开自动合并前打；`autopilot:auto-merged` 由 watch 在合并后打。
- CLI：`run` 加 `--notify <会话名>`、`--author-host`；新增 `watch`。
- 本机：`~/.autopilot/config.json` 新增 `mergeMode`（默认 `automerge`）；新增 `~/.autopilot/watch-state.json`、`~/.autopilot/watch.lock`。
- 风险分级结果新增 `human_only`、`human_reasons`；`auto_merge_eligible` 语义收紧。
- 证据评论新增 `route`、`routeReasons`、`validatedHeadOid`。

## 风险与回滚

| 风险 | 对策 |
|---|---|
| 风险分级放宽后坏代码被自动合并 | 合并前验证（回归用例）和 `CI required` 仍是必需；保护性升档保留为 `human_only`；先在沙盒跑 S1～S13 |
| watch 没装或停了，自动合并后没人收尾 | 路线判定要求 watch 心跳新鲜，否则走人工路；合并本身在 GitHub 完成不受影响 |
| 打开自动合并后又推提交 | `autopilot/verified` 绑定提交（第一道）；watch 核对 head 关闭（第二道） |
| 主干变红漏判（authored 标签） | 打开自动合并前就打 authored |
| 降级 issue 重复（旧 workflow 与 watch 并存） | 复用 reason+ref 去重 |
| delegate 执行 ai-operate 结果不稳 | 解析不到即「不能执行」转人工；第一版不自动执行会动全局状态的用例 |
| 分类判错 | 只有明确的环境信号才算 env，其余按 test |

回滚按总体方案第 9 节：`watch --freeze` → 冻结后第一轮关掉所有排队的自动合并 → `mergeMode: direct` → 卸 launchd → spec-guard `takeover` 改回 `none`。每步在沙盒可单独验证（S12）。

## 方案取舍

- **路线判定放在 run 里，而不是读 GitHub 的必需检查后由 GitHub 决定**：GitHub 只管「能不能合」，不知道档位、T4、watch 是否在线；hub 必须先判定。
- **验证不通过不写 failure 状态**：写 failure 会让 PR 页面显示红叉，但「没跑过」也是缺状态，两者对合并的效果一样；只写 success 更简单，原因在证据评论里。
- **ai-operate 用 delegate run 而不是新开会话**：delegate 已有执行通道和后端选择（总体方案 4.3b 已定）。
- **watch 用一个进程巡检所有仓库，而不是每仓一个**：总需求 Q1 已定一个总的；共用锁和状态文件更简单。
- **主干变红不在 degrade workflow 里分类**：分类要重跑和读 job 详情，需要 Actions 写权限和重试，放在本机 watch 更好控制；workflow 只保留撤回和回归标签这类确定事件。
- **不做 hub 的推送受控入口**：Q6 已定交 A10。
