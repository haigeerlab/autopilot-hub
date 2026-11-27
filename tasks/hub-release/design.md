# 技术方案：hub-release

<!-- spec-guard: review pending -->

> 依据：`spec/hub-release.md`（已批）；总账 `research/2026-11-26-自动驾驶目标/` 的总体技术方案（已批）第 4.2、4.5、4.6、4.8、6、8 节与 `打tag授权办法-草稿.md`（用户选 A）。本模块是重要需求，本方案交第二审查者审一轮后由用户批。

## 现状调研

查了：`pipeline/engine.py`、`pipeline/entry.py`、`pipeline/limits.py`、`pipeline/model.py`、`pipeline/templates/plugin.yml`、`release-limits.json`、`orchestrator/cli.py`（`run_release`、`release_checkout`、`release` 子命令、map_view 调用）、`orchestrator/watch.py`（主干巡检、发版前提的状态）、`orchestrator/state.py`、`orchestrator/flow.py`（`view_merge`、证据块）、spec-guard 0.101.0 的 `hooks/map_view.py`（`takeover`、`overallDesign`、模块状态）。没查：各插件仓库的 `interface.yml` 与 `release-tag.yml` 细节（交接清单里由各家落地，端到端验收时核对）。

- **发布引擎入口** `engine.run(io, repo, version=None, *, resume, confirm, dry_run)`（`engine.py:171`）：新发版必须给 `version`（:183-187），读的是**当时的主干头**（:197-198），`resolve_pipeline` 也按它解析；续跑按 issue 里 `autopilot-release` 块记下的 `commit` 执行（:218、:266），但流程定义仍按当时主干头重新解析并比对摘要（:199-205）。
- **档位**：`read_release_level`（:19）只读**项目级** `takeover.release`，模块覆盖和收窄都不看；`effective_level`（`limits.py:28`）再按上限取低。`release-limits.json`：`pipeline_own` 四个（spec-guard、module-crew、delegate、agent-relay）一律压到 `prepare`（:33-35），`repo_caps` 只能往严里压（:47-54），kb 为 `auto-confirm`。
- **逐步判定** `decision`（:49）：每步按自己的 `auto_when` 与当前档位比；`--confirm` 只放行当前 `waiting` 的那一步（:225-234、:258-260），后面的步骤照样按档位判。
- **执行环境** `run_release`（`cli.py:395`）：`/bin/sh -c` 在一次性克隆里跑，环境由 `_clean_env()` 去掉 GitHub 令牌，另给 `AUTOPILOT_VERSION`、`AUTOPILOT_REPO`、`AUTOPILOT_RELEASE_ISSUE`、`AUTOPILOT_HUB`；单步 30 分钟超时（`RELEASE_TIMEOUT`，:57）。克隆的 `origin` 是不带凭据的 https 地址，并已抓取全部 tag（:666-675）。
- **入口**：`entry.py` 只按名字读 `.github/autopr/interface.yml` 的 `entries.<name>.run` 并原样执行。
- **插件模板**（`templates/plugin.yml`）：`impact`（合并后，human 档也自动）→ `full-test` → `prepare` → `approve`（要 `auto-confirm`）→ `tag-and-release`（调 `release_publish`，打 tag 和建 Release 在一步里）→ `install-local` → `verify-install` → `restart-codex`、`reload-claude`（`by: human`）→ `record`。
- **spec-guard 视图**：`/map --json` 每个模块有 `status`、`takeover`（含 `release`、`releasePreauth`、`narrowed`、`source`）、`overallDesign`（`{path, status}` 或 `null`）。hub 已有调用 map_view 的入口（`cli.py:155-159`、:805、:840）。
- **watch 主干巡检**（hub-auto-merge）：主干 push 运行变绿时记 `runs[...]`：`headSha`、关联 `pr`、`validatedHeadOid`、`evidenceUrl`；PR 的 v2 证据评论块里有 `module`。仓库级 `narrowed`、`frozen`、降级 issue 已有读法（`flow.automerge_hold_reasons`、`active_degrade_issues`）。
- **已有的确认入口**：`cli.py release --resume N --confirm <原话>` 由会话传入，引擎不核对 issue 里的评论（需求要求改掉这一点）。

## 影响面

- `pipeline/engine.py`：入口加 `target`；先只读跑 `impact` 定版本；按模块判档与授权；确认核对；打 tag 前后核对；Release 就绪核对；持久状态与幂等。
- `pipeline/release_gate.py`（新）：发布内容、模块归属、需求分组、授权与作废判定、确认解析，都是纯函数。
- `pipeline/limits.py`、`release-limits.json`：上限放宽规则（T4）。
- `pipeline/templates/plugin.yml`：`tag-and-release` 拆成 `tag-local`、`push-tag`、`await-release` 三步（T4，hub 内置模板）。
- `orchestrator/cli.py`：`run_release` 加 `AUTOPILOT_RELEASE_VERSION`、`AUTOPILOT_RELEASE_TARGET`；`release` 子命令加 `--target`；新增 App 推 tag 的短期令牌（`RELEASE_TAG = {"contents": "write"}`）；`release_checkout` 记录执行前的 tag 列表。
- `orchestrator/watch.py`、`orchestrator/state.py`：主干绿后触发发版；`releases[...]` 状态、`lastReleasedSha`；发版前提检查；暂停与恢复；24 小时等人时限。
- `orchestrator/flow.py`：只读复用 `automerge_hold_reasons`、`active_degrade_issues`、风险分级（作废判定）。
- `tests/test_release_gate.py`（新）、`tests/test_pipeline_engine.py`、`tests/test_watch.py`、`tests/test_limits.py`。
- spec-guard `hooks/module_scope.py`：只读调用，取 `scope_base`。
- 文档：`README.md`、`README.en.md`（发版现状、「打开自动发版之前」清单）、`CHANGELOG.md`、`CLAUDE.md`、`AGENTS.md`（职责边界 hub 两句，已批措辞）。
- 不动：各插件仓库（交接清单）、`release-tag.yml`、npm／site 模板、准入与合并路线。

## 实现方案

### 1. 发版入口与目标提交（4.5）

- `engine.run(io, repo, *, target=None, version=None, resume=None, confirm=None, dry_run=False)`：新发版必须给 `target`（40 位）；`version` 改为可选（由第 3 节算出），给了就和算出的比对，不一致就停。`resolve_pipeline`、`read_release_level` 全都用 `target`，不再读当时主干头。`autopilot-release` 起始块记 `target`、`version`、清单指纹；续跑只用块里的值。
- `cli.py release --repo R --target SHA [--version V]`；续跑 `--resume N`（见第 4 节的 `--confirm`）。
- watch 触发：主干 push 运行变绿（`runs` 记为 `green`）、且这个提交的必需检查全部成功时，把它作为候选目标，进第 2 节判定；同一仓库已有进行中的发版就排队，不并发。

### 2. 发布内容、需求分组与判定（`release_gate.py`）

- **上次发版提交**：`state.repos[R].lastReleasedSha`；没有时取目标提交之前（`git describe --tags --abbrev=0 <target>^`，只认 `v*`，不含本次 tag）最近版本 tag 指向的提交；一个都没有 → 首次发版，路线 `human`。人批交接时由交接方传 `--tag vX.Y.Z`，上次发版取它之前最近的版本 tag。
- **不倒序**：目标不是上次发版提交的后继（`git merge-base --is-ancestor`）→ 记「已覆盖」，不发。
- **发布内容的 PR**：在一次性克隆里列 `上次..目标` 的首父提交，对每个提交用 `GET /repos/R/commits/{sha}/pulls` 找已合并 PR；PR 的 v2 证据块给 `module`；找不到 PR 或块里没 `module` → 记一个「未归属」项。
- **需求分组**：在目标提交上跑 spec-guard `map_view.py --json`。发布内容涉及的模块按 `overallDesign.path` 分组，没有 `overallDesign` 的单独成组；每组的模块必须全部 `status: done`，且每个模块的最后一个合并 PR 的合并提交是目标的祖先，否则这次不发（记「需求未做完」，等下一轮）。
- **判定**（按需求的先后）：
  1. 任一涉及模块 `takeover.release == human` → `route = human`；
  2. 否则任一模块 `takeover.release` 低于 `auto-confirm`、`releasePreauth is not True`、命中作废、取不到开工基线，或有「未归属」项 → `route = confirm`（按 `prepare` 停下）；
  3. 否则 `route = auto`。
- **作废**：开工基线取 spec-guard `hooks/module_scope.py` 给出的模块 `scope_base`（hub 的「不许改裁判」已用同一入口，`Work.changed_existing_files`）；取不到、或不是目标的祖先，按作废；用 `risk/tiers.py` 对 `scopeBase..target` 的改动判 T4；升主版本由第 3 节的 impact 结果给。
- 判定结果连同每个模块的事实写进汇报和发版跟踪 issue 的起始块。

### 3. 先定版本号（只读）

- 引擎在判定之前，在一次性克隆里先跑模板的 `impact` 步（它本来就是 `auto_when: "release: human"`、只读），要求输出一行 `IMPACT: {"level": "major|minor|patch", "version": "X.Y.Z"}`；读不到、解析不了、版本号不是语义版本 → 停下交用户。`impact` 由 `entry.py impact` 执行仓库登记的入口；登记契约里补这一行输出（列入交接清单第 2 项，旧入口没有这一行时按停下处理）。
- `level == major` → 作废，`route` 至少为 `confirm`。

### 4. 档位、确认与一次放行

- `route = auto`：本次档位 `auto-confirm`；`route = confirm`：开 issue，档位 `prepare`，停在 `approve` 之前；`route = human`：档位按 `human`，`tag-local`／`push-tag` 不做，等交接方给 tag（第 7 节）。
- 停下时 issue 起始块写清单：`[{id, at, by, run, needsUser}]`，`needsUser` 为 `by: human` 或 `ask:` 的步骤；指纹 `sha256(json.dumps({target, version, steps}, sort_keys=True))`，正文展示前 8 位。
- **确认解析**（纯函数）：只读 `user.id == 仓库所有者 id` 且 `user.type == "User"`、`created_at == updated_at`、`id` 大于最新清单评论的评论；逐行看，引用行（`>` 开头）跳过，正好匹配 `确认发版 <8 位十六进制>` 且等于当前指纹前 8 位才算。仓库 `owner.type != "User"` → 停下（不支持组织仓库）。
- 确认成立后本次档位升到 `auto-confirm`，并记 `confirmed: {commentId, fingerprint}`；之后不再逐步停，`needsUser` 的步骤照样 `waiting`。
- `--confirm` 只用于 `needsUser` 步骤做完后继续；用在 `approve` 或推 tag 前一律拒绝（提示到 issue 里回复）。

### 5. 打 tag（App 身份，单个 tag）

- `tag-local`：在一次性克隆里执行 `entry.py release_publish`，`run_release` 额外给 `AUTOPILOT_RELEASE_VERSION`（同 `AUTOPILOT_VERSION`）和 `AUTOPILOT_RELEASE_TARGET`；环境里仍没有任何令牌，`origin` 也不带凭据。执行前后各列一次 `git tag -l 'v*'` 与 `git for-each-ref refs/tags`：恰好多出一个 `v<版本>`、`git rev-parse v<版本>^{commit} == target`、是附注 tag，才算通过；否则停下。
- `push-tag`：授权再核一次（`route == auto` 或 `confirmed` 有效）；查远端 `git ls-remote origin refs/tags/v<版本>`：已有且解引用等于 `target` → 视为已推；已有但不等 → 停下；没有 → 用新令牌 `RELEASE_TAG`（`contents: write`，按仓库签发、用完撤销）执行 `git push origin refs/tags/v<版本>:refs/tags/v<版本>`，只这一个引用；推后读回再核一次。
- `route == human` 时这两步跳过，由交接方推。

### 6. 等 Release 就绪

- `await-release`：轮询（每 30 秒，单步总时限 30 分钟）`GET /repos/R/actions/runs?event=push&head_sha=<target>` 里 `release-tag.yml` 对应 `v<版本>` 的那次运行，要求 `status == completed` 且 `conclusion == success`；再 `GET /repos/R/releases/tags/v<版本>` 存在、`tag_name` 一致、`target_commitish` 或 tag 指向等于 `target`。运行失败（即使 Release 已建）、没触发、超时 → 停下。
- spec-guard 补上 `release-tag.yml` 之前：`release-limits.json` 加 `release_creator: {"haigeerlab/spec-guard-plugin": "local"}`，这类仓库 `route` 只能是 `human`；`await-release` 只核对 Release 存在、对应 tag。

### 7. 人批交接

- 交接方（module-crew 或 spec-guard 会话）推完 tag 后调 `cli.py release --repo R --tag vX.Y.Z`：hub 读 tag 指向的提交作为 `target`，跳过 `tag-local`、`push-tag`，从 `await-release` 开始。上次发版提交取这个 tag 之前最近的版本 tag。

### 8. 状态、暂停与幂等（4.6）

- `watch-state.json` 新增 `releases["owner/repo"]`：`{state: planned|running|waiting-human|done|paused|covered, target, version, route, issue, step, since, pause, intents}`，以及 `repos[R].lastReleasedSha`。
- 每个副作用（开 issue、推 tag、发评论、发通知、关 issue）先写意图和幂等键 `R:release:<target>:<version>:<动作>`，做完写 `done`；中断后按意图对账远端（tag 是否在远端、指向哪里，issue 是否已开、评论是否已发）。
- 发版前提：`frozen`、`repos[R].narrowed`、`repos[R].pause`、有未关闭的降级 issue → 不开始；进行中的在下一步前停，记 `paused`，原因写明。
- 时限：单步 30 分钟（沿用 `RELEASE_TIMEOUT`）；`waiting-human` 超过 24 小时 → `paused`，通知用户和会话；用户确认或做完仍可继续（`--resume` 或 issue 确认）。
- 完成顺序：先记 `lastReleasedSha = target` 与账本 `release_done`，再发总结评论、关 issue。

### 9. 上限（T4，最后上线）

- `effective_level(repo, raw, pipeline, limits, *, authorized=False)`：`authorized` 为真（`route == auto` 或已确认）时，`pipeline_own` 不再强压 `prepare`，上限取 `auto-confirm`；`repo_caps` 仍照旧取低；上限文件缺失或读不懂仍按 `prepare`。`authorized` 为假时行为与现在完全一样。
- `release-limits.json` 加 `repo_caps` 不变，新增 `authorized_cap: "auto-confirm"`（版本号 `schema` 升 v2，读到 v1 按现状）。这一步放在最后一个实现 PR，等授权核对上线后由用户合并。

### 10. 发版后发现问题

- `verify-install` 失败 → 现有暂停与通知，通知里写版本、tag、Release 链接。
- 发版后主干 CI 变红：watch 的主干巡检已在处理，通知里补「最近一次已发版本 vX.Y.Z（目标提交 …）」。
- `cli.py report-failure --repo R --pr 0 --stage release --detail "vX.Y.Z …"`：沿用 hub-auto-merge 的入口，记暂停并通知用户。

## 接口和数据改动

- `cli.py release`：新增 `--target`、`--tag`；`--version` 改为可选；`--confirm` 限定只用于 `needsUser` 步骤。
- 发布执行环境：新增 `AUTOPILOT_RELEASE_VERSION`、`AUTOPILOT_RELEASE_TARGET`（旧的 `AUTOPILOT_VERSION` 保留）。
- `impact` 入口输出契约：`IMPACT: {"level", "version"}` 一行。
- `release_publish` 入口契约：只在本地建附注 tag `v<AUTOPILOT_RELEASE_VERSION>` 指向 `AUTOPILOT_RELEASE_TARGET`。
- 插件模板步骤：`tag-and-release` → `tag-local`、`push-tag`、`await-release`。
- issue 起始块 `autopilot-release`：新增 `target`、`route`、`fingerprint`、`modules`、`confirmed`。
- `watch-state.json`：`releases`、`repos[R].lastReleasedSha`。
- `release-limits.json`：`schema` v2，新增 `authorized_cap`、`release_creator`。
- 新令牌分组 `RELEASE_TAG = {"contents": "write"}`。

## 风险与回滚

| 风险 | 后果 | 应对 |
|---|---|---|
| App 令牌能推 tag | 令牌泄露可伪造版本 | 只在 `push-tag` 签发、只推一个核对过的引用、用完撤销；六仓 tag 规则集只允许 App 与用户本人 |
| 判定读错 `/map --json` 字段 | 不该自动的被自动发 | 读不到、类型不对一律按「没有授权」；spec-guard 接口版本低于要求时 `route` 至少 `confirm` |
| `impact` 入口没有新输出 | 判不了主版本 | 读不到就停下交用户，不默认 patch |
| `release-tag.yml` 没触发 | 发版卡住 | 30 分钟后暂停并通知；不由 hub 建 Release |
| 中途中断 | 重复推 tag 或漏关 issue | 意图 + 幂等键，推前读远端 |

回滚：`release-limits.json` 去掉 `authorized_cap`（或用户把模块 `takeover.release` 收回 human）即回到全部停下确认；watch `--freeze` 停止开始新发版；最坏把 hub 钉回本模块之前的提交。已发出的版本不撤回。

## 验收方式（对应需求成功标准）

| 成功标准 | 怎么验 |
|---|---|
| S1、S2 | 沙盒：一次提前授权的自动发版、一次停下确认的发版（端到端，用户原话） |
| S3～S6 | `test_release_gate.py`：T4、主版本、修两次、多模块、人批优先、未归属、需求未做完、合并不在目标里 |
| S7、S8 | 确认解析与推 tag 前核对的单元测试（引用行、指纹不符、编辑过、非所有者、组织仓库、`--confirm` 被拒） |
| S9 | 引擎测试：执行环境无令牌、多出 tag 数量与指向核对、远端同名 tag 指向别处时停下 |
| S10 | `await-release` 测试：运行成功且 Release 匹配才继续；运行失败但 Release 已建时停下 |
| S11、S12 | watch 测试：目标固定、续跑不读主干头、已覆盖、串行、首次发版人批、人批交接不把本次 tag 当上次、收紧／冻结／降级不发 |
| S13 | 状态测试：30 分钟、24 小时、中断后对账不重做 |
| S14 | 通知测试：安装验证失败、主干变红、`report-failure --stage release` |
| S15 | `test_limits.py`：授权为假时与现在一致；文件缺失按 `prepare`；`repo_caps` 仍取低 |
| S16 | 人批交接测试：跳过打 tag，从 `await-release` 开始 |

## 方案取舍

- **授权判定放在 hub 侧纯函数，不交 spec-guard**：spec-guard 只给事实（档位、授权、状态、分组），发布内容和 PR 归属只有 hub 知道。
- **先跑 impact 再判授权**：impact 本来就只读、human 档也自动；放在判定前不增加风险。
- **tag 在本地建、由 hub 推**：保留现有「发布步骤不给推送凭据」的边界；只多一个专用令牌、一个引用。
- **不在 hub 建 Release**：需求定了 Release 只由仓库 `release-tag.yml` 建；hub 只等、只核对。
- **上限最后放宽**：授权核对没上线前，任何仓库都不会因本模块变成自动发版。

## 已定的偏离与外部前提

- **新增的外部前提（需求交接清单没有列，随本方案交用户批）**：各仓库 `impact` 入口输出一行 `IMPACT: {"level": "major|minor|patch", "version": "X.Y.Z"}`。需求要求「升主版本就作废」，但现在没有任何仓库的 impact 入口给出这个结论，hub 判不了；没有这一行的仓库按停下交用户处理。批准后补进交接清单，交 ecosystem-lead 转发。
- 外部前提（需求交接清单已列）：各仓登记 `release_publish`；spec-guard 补 `release-tag.yml`；六仓 tag 规则集。前提不满足的仓库，`route` 最多 `confirm` 或 `human`。

## 审查记录

## 变更记录
