# 技术方案：hub-release

<!-- spec-guard: review pending -->

> 依据：`spec/hub-release.md`（已批）；总账 `research/2026-11-26-自动驾驶目标/` 的总体技术方案（已批）第 4.2、4.5、4.6、4.8、6、8 节与 `打tag授权办法-草稿.md`（用户选 A）。本模块是重要需求，本方案交第二审查者审后由用户批。

## 现状调研

查了：`pipeline/engine.py`、`pipeline/entry.py`、`pipeline/limits.py`、`pipeline/model.py`、`pipeline/templates/plugin.yml`、`release-limits.json`、`orchestrator/cli.py`（`_clean_env`、`run_release`、`release_checkout`、`release` 子命令与令牌、可信 v2 证据筛选、PR 正文编辑判断）、`orchestrator/watch.py`、`orchestrator/state.py`、`orchestrator/flow.py`、spec-guard 0.101.0 `hooks/map_view.py`、`hooks/module_stage.py`；五个插件仓库主干上的 `.github/autopr/interface.yml`。

- **发布引擎** `engine.run`（`engine.py:171`）：新发版必须给 `version`（:183-187），读**当时主干头**（:197-198）并按它 `resolve_pipeline`；续跑按 issue 起始块的 `commit` 执行（:218、:266），流程定义仍按当时主干头重解析比对（:199-205）。起始块只信机器人发的、未编辑的 issue 正文（:87-101），已关闭的 issue 不能续跑（:99）。
- **档位**：`read_release_level`（:19）只读项目级 `takeover.release`；`effective_level`（`limits.py:28`）：`pipeline_own` 直接压到 `prepare`、不再看 `repo_caps`（:33-35），其余按模板类别算上限后再用 `repo_caps` 取低（:47-54）。`release-limits.json`：`pipeline_own` 为 spec-guard、module-crew、delegate、agent-relay；kb 的 `repo_caps` 为 `auto-confirm`。
- **逐步判定** `decision`（:49-64）：`ask:`、`by: human`、`by: external`、没写 `auto_when`、档位不够，都等人；`--confirm` 只放行当前等待的一步（:225-234、:258-260）。
- **执行环境** `run_release`（`cli.py:395`）：`/bin/sh -c`，`_clean_env()`（`cli.py:191`）只去掉 `GH_TOKEN`、`GITHUB_TOKEN`、`GH_ENTERPRISE_TOKEN`，其余环境（含 `HOME`、git 凭据助手、`gh` 登录、`SSH_AUTH_SOCK`）照旧继承；克隆 `origin` 是不带凭据的 https，但会抓全部 tag（:666-675）。`release` 子命令的令牌只有 `contents: read`、`issues: write`（:1199），没有 Actions 读。
- **入口**：`entry.py` 只按名字执行 `interface.yml` 的 `entries.<name>.run`，没登记返回 3。
- **插件模板**：`impact`（合并后，human 档也自动）→ `full-test` → `prepare` → `approve` → `tag-and-release`（调 `release_publish`、要 `url` 证据；就地打 tag／推送／建 Release 的实际行为由仓库命令决定）→ `install-local` → `verify-install` → `restart-codex`、`reload-claude`（`by: human`）→ `record`。仓库可 `remove`／`replace` 模板步骤，引用不存在的 id 报错（`model.py:140-175`）。
- **各仓入口现状**（主干）：只有 delegate 登记了 `impact`（打印上一个标签、提交列表、接口版本，不给升级结论）和 `release_prepare`；agent-relay 只登记 `check`；kb、spec-guard、module-crew 没有 `interface.yml`。没有任何仓库登记 `release_publish`。
- **提交规范**：hub 规则 F1（`rules/commit_format.py:29-30`）要求提交第一行「类型(范围)!?: 说明」，`!` 表示不兼容。
- **可信证据**：`cli.py:1236-1248` 已有「机器人发、绑定 PR head 的 v2 证据块」筛选，块里有 `module`；PR 正文是否编辑过用 GraphQL `lastEditedAt` 判（`cli.py:700-709`，同一秒的编辑也能识别）。
- **spec-guard 视图**：`/map --json` 每个模块有 `status`、`takeover`（含 `release`、`releasePreauth`、`narrowed`）、`overallDesign`；没有开工基线字段，开工基线由 `hooks/module_scope.py` 给（hub 的「不许改裁判」已在用，`cli.py:466`、:486）。
- **watch**：主干 push 运行处理完记 `runs[...]`（`watch.py:439-440`、:557-576），主干运行的暂停写在该运行的 `fields.pause`（:532-539）；仓库级 `repos[R].pause` 只记巡检本身出错（:747-748）。watch 持锁跑整轮（:662-667）。`report-failure` 按 PR 记暂停（:42-63）。修复次数在 `state.repairs`（`state.py:190-224`）。

## 影响面

- `pipeline/release_gate.py`（新）：发布范围、模块归属、需求分组、路线判定、版本号计算、确认解析。纯函数。
- `pipeline/engine.py`：入口与资格、目标提交、守卫步骤、可信清单与确认记录、最终档位、状态驱动的续跑与收尾。
- `pipeline/limits.py`、`release-limits.json`：授权放宽（v2，T4，最后上线）。
- `pipeline/templates/plugin.yml`：`tag-and-release` 改为只在本地建 tag（id 保留）；推 tag、等 Release 由引擎内置守卫步骤做，不在模板里、不可被仓库移除或替换。
- `orchestrator/cli.py`：`release` 子命令 `--target`、`--tag`；隔离执行环境；按动作分组的令牌；GraphQL 读评论编辑记录。
- `orchestrator/watch.py`、`orchestrator/state.py`：候选入队、发版锁、`releases`、`released`、`lastReleasedSha`、守卫读取、暂停与通知。
- spec-guard `hooks/map_view.py`、`hooks/module_scope.py`：只读调用。
- 测试：`tests/test_release_gate.py`（新）、`tests/test_pipeline_engine.py`、`tests/test_watch.py`、`tests/test_limits.py`、`tests/test_state.py`。
- 文档：`README.md`、`README.en.md`、`CHANGELOG.md`、`CLAUDE.md`、`AGENTS.md`（职责边界 hub 两句，已批措辞）。
- 不动：各插件仓库（交接清单）、`release-tag.yml`、npm／site 模板、准入与合并路线。

## 实现方案

### 1. 入口、资格与目标提交

- `engine.run(io, repo, *, target=None, tag=None, version=None, resume=None, confirm=None, dry_run=False)`。新发版必须给 `target`（40 位）或 `tag`（人批交接，见第 9 节）。
- **资格**（引擎入口统一核，watch 和命令行共用）：仓库在 `release-limits.json` 的 `release_repos`（五个插件仓库与沙盒）里，不是 hub；`owner.type == "User"`；`target` 在默认分支历史上；`target` 上主干规则要求的必需检查全部成功（复用 `flow.verified_requirements` 读规则，按提交的检查结果核对）。任一不满足 → 停，不开 issue。
- `resolve_pipeline`、档位、守卫全部基于 `target`；起始块记 `target`，续跑只用起始块的值。

### 2. 发布范围、模块归属与需求分组（`release_gate.py`）

- **上次发版提交**：`state.repos[R].lastReleasedSha`；没有时取 `git describe --tags --abbrev=0 --match 'v[0-9]*' --exclude <本次 tag> <target>`（考虑全部祖先）的 tag 解引用提交；一个都没有 → 首次发版，`route = human`。
- **先后**：`target` 是上次的后继 → 继续；`target` 是上次的祖先或相等 → `covered`；两者分叉 → 停，记暂停交用户（不当覆盖）。
- **范围内的 PR**：`git rev-list 上次..target`（全部提交，不只首父）；每个提交用 `GET commits/{sha}/pulls` 找 PR，要求 PR 已合并、`base.ref` 为默认分支、`merge_commit_sha` 在范围内；模块取「机器人发、绑定 PR head、未编辑」的最新 v2 证据块（复用 `cli.py:1236-1248` 的筛选）的 `module`。找不到合格 PR 或证据的提交 → 「未归属」。
- **需求分组**：在 `target` 上跑 `map_view.py --json`。模块按 `overallDesign.path` 分组，组内模块须全部 `status: done`，且每个模块所有已合并 PR 的合并提交都是 `target` 的祖先。**没有总体技术方案的模块**：能力图上另有已开工未完成的模块时，无法判断是否同一需求，`route` 至少 `confirm`，汇报写明（偏离见「已定的偏离」）。
- **入口可用性**：`target` 上 `interface.yml` 没登记 `release_publish`，或没有 `.github/workflows/release-tag.yml`，或 `release-limits.json` 的 `release_creator[R] == "local"` → `route = human`。

### 3. 版本号（只读，不靠仓库输出）

- `impact` 步照旧执行（只读、记证据），但版本号由 hub 自己算：读上次版本 tag 的版本号，再读范围内每个提交的第一行（F1 格式）：有 `!` 或正文有 `BREAKING CHANGE:` → 主版本 +1；否则有 `feat` → 次版本 +1；否则修订号 +1。提交不合 F1 格式 → 停下交用户。
- 是否升主版本 = 新版本主号 > 上次主号，独立于提交类型再核一次。
- 命令行给了 `--version` 时与算出的比对，不一致就停。人批交接（`--tag`）的版本取自 tag 名，不再计算，但仍记录是否升主版本供汇报。

### 4. 路线判定与最终档位

- `route` 按需求规定的先后：任一模块 `takeover.release == human`、或第 2 节判为 human → `human`；否则任一模块 `takeover.release` 低于 `auto-confirm`、`releasePreauth is not True`、作废（`module_scope.py` 的 `scope_base` 取不到或不是 `target` 祖先、`scope_base..target` 含 T4、升主版本）、有未归属、或第 2 节判为至少 `confirm` → `confirm`；否则 `auto`。
- **最终档位只有一个函数** `final_level(route, confirmed, pipeline, limits)`：`human` → `human`；`confirm` 未确认 → `prepare`；`auto` 或已确认 → `min("auto-confirm", effective_level(..., authorized=True))`。最终档位低于 `auto-confirm` 而本该自动 → 停下并写原因（例如「上限文件压到 prepare」）。推 tag 守卫再算一次。

### 5. 可信清单与确认

- 停下确认时，机器人在 issue 里发一条**新评论**（不改正文），块 `autopilot-release-checklist`：`{seq, target, version, route, reasons, steps:[{id, at, by, run, needsUser}], fingerprint}`；`needsUser` = 该步在 `auto-confirm` 档下 `decision` 仍为等待（含 `ask:`、`human`、`external`、无 `auto_when`、要求高于 `auto-confirm`）。`fingerprint = sha256(规范化 JSON(target, version, steps))`。清单有变就发 `seq+1` 的新清单，旧确认作废。
- **确认判定**：评论作者 `user.id == 仓库所有者 id`、`user.type == "User"`；GraphQL `IssueComment.lastEditedAt` 为空（未编辑）；评论 id 大于最新清单评论；评论正文去掉首尾空白后**整条恰好是** `确认发版 <指纹前 8 位>`（多一行、引用、别的文字都不算）。
- 确认成立后，机器人发块 `autopilot-release-confirmed {seq, fingerprint, commentId}`。之后按 `auto-confirm` 跑，`needsUser` 的步骤照样等人。
- `--confirm` 只能放行当前等待且 `needsUser` 的步骤；当前等待的是 `approve` 或推 tag 时拒绝，提示到 issue 回复。

### 6. 本地建 tag 与推 tag（守卫步骤）

- **执行顺序**：先对账远端（`git ls-remote origin refs/tags/v<版本> refs/tags/v<版本>^{}` 取解引用提交）：已有且等于 `target` → 跳过建和推，直接到第 7 节；已有但不等 → 停；没有 → 往下。
- **`tag-and-release`（模板步，改为本地建 tag）**：在**隔离克隆 A** 里执行 `entry.py release_publish`。环境：新建临时 `HOME`；`GIT_CONFIG_NOSYSTEM=1`、`GIT_CONFIG_GLOBAL=/dev/null`、`GIT_TERMINAL_PROMPT=0`、`GIT_ASKPASS=/bin/false`；去掉 `SSH_AUTH_SOCK`、`GH_*`、`GITHUB_*`、`GIT_*` 凭据类变量；另给 `AUTOPILOT_RELEASE_VERSION`、`AUTOPILOT_RELEASE_TARGET`（保留旧 `AUTOPILOT_VERSION`）。执行前后各记一次 `git for-each-ref refs/tags --format='%(refname) %(objectname)'`：只允许新增 `refs/tags/v<版本>` 一条、其他引用不增不改；新引用的对象是附注 tag（`git cat-file -t` 为 `tag`），`git rev-parse <对象>^{commit} == target`。通过后记下 tag 对象 id 与对象内容（`git cat-file tag`）。
- **`push-tag`（引擎内置守卫步，不在模板里）**：重新核授权（`route == auto` 时在 `target` 上重算第 2、4 节；`confirm` 时重读最新清单与确认原评论、再判一次）；在 hub 自己的**克隆 B**（全新、不执行任何仓库代码）里，从克隆 A 只取那一个 tag 对象（`git fetch <A> <对象 id>`），核对对象 id 一致；用新令牌 `RELEASE_TAG = {"contents": "write"}` 执行 `git -c core.hooksPath=/dev/null push --no-verify origin <对象 id>:refs/tags/v<版本>`；推后用 `ls-remote` 读回，解引用等于 `target`、对象 id 一致。令牌用完撤销。
- `route == human` 时这两步跳过。

### 7. 等 Release 就绪（守卫步）

- `await-release`：令牌 `RELEASE_READ = {"actions": "read", "contents": "read"}`。每 30 秒查一次，单步总时限 30 分钟：`release-tag.yml` 对 `v<版本>` 的那次 push 运行 `completed` 且 `success`；`GET releases/tags/v<版本>` 存在、`tag_name` 一致、名字含版本号；远端 tag 解引用等于 `target`。运行失败（即使 Release 已建）、没触发、超时 → 停。
- `release_creator[R] == "local"`（spec-guard 补 workflow 前）：只走人批，跳过运行核对，只核 Release 与 tag。

### 8. 状态、锁、暂停与收尾（4.6）

- **锁与排队**：发版用每仓库独立的文件锁 `~/.autopilot/release-<owner>__<repo>.lock`，命令行和 watch 都先拿它。watch 只做「入队」：主干运行变绿且通过资格 → `releases[R].candidates` 追加 `target`（去重、按提交顺序），持久化后才推进游标；watch 在自己的整轮锁之外，对每个有候选且没在跑的仓库启动一个独立的 `cli.py release --repo R --target <最早候选>` 进程，不在 watch 锁内跑长步骤。
- **记录**：`releases[R] = {state, target, version, route, issue, step, since, pause, candidates}`；`state` 为 `planned|running|waiting-human|finishing|done|paused|covered`。`released[R][version] = {target, tagObject, releaseUrl, issue, at}` 在推 tag 成功时写，Release 就绪后补 `releaseUrl`。
- **幂等**：意图键 `R:release:<target>:<version>:<步骤>:<动作>[:<对象>]`（对象如评论 seq、通知对象），先记意图后做、做完写 done。中断后按意图对账：tag 看远端；issue／评论按块里的键查；`install-local` 先读本机已装版本，等于 `version` 视为已做；`record` 按 `version` 去重。
- **守卫**（开始前和每步之前）：`frozen`；`repos[R].narrowed`；该仓库主干最新运行的 `fields.pause` 或未解决的红色分类；`repos[R].pause`；未关闭的降级 issue。任一成立 → `paused`，记原因。
- **失败**：任一步 `stopped` → `paused`，通知用户（桌面 + issue 评论）和负责的会话（第 10 节）；时限：单步 30 分钟，`waiting-human` 24 小时 → `paused`。离开暂停：用户在 issue 确认、命令行续跑或 `watch --resume R`。
- **收尾**：Release 就绪、安装与验证完成后先置 `finishing`、写 `lastReleasedSha = target`、`released`、账本 `release_done`；再发总结、关 issue、`done`。`finishing` 状态下续跑只补收尾，不做覆盖判定。

### 9. 人批交接

- `cli.py release --repo R --tag vX.Y.Z`：读远端 tag 解引用提交作为 `target`，过第 1 节资格（不要求提前授权），`route = human`，跳过建／推 tag，从 `await-release`（或 `release_creator == local` 时的 Release 核对）开始；上次发版提交取这个 tag 之前最近的版本 tag。

### 10. 汇报与通知

- **开始前**：写账本事件「将要做什么」：仓库、目标、版本、路线、清单、每个模块的授权与作废事实、范围内 PR 的修复次数（`state.repairs`）。通知负责的会话：取范围内 PR 在 `state.prs` 里的 `notify`，去重；没有就只通知用户。
- **发版后发现问题**：`verify-install` 失败 → 暂停并通知，附 `released[R][version]` 的 tag、Release、issue；发版后主干 CI 变红 → watch 的主干通知里补「最近已发版本」（取 `released[R]` 最新一条）；`cli.py report-failure --repo R --stage release --version vX.Y.Z --detail …`：按版本查 `released` 补齐证据后通知用户（`--pr` 在 `--stage release` 时可省）。

### 11. 上限（T4，最后一个实现 PR）

- `effective_level(repo, raw, pipeline, limits, *, authorized=False)`：只有 `authorized` 为真**且** `release-limits.json` 为 v2 并给出 `authorized_cap` 时，`pipeline_own` 的上限改取 `authorized_cap`（只能是 `auto-confirm`），再与 `repo_caps` 取低；其余情况与现在完全一样。文件缺失、读不懂、v1 → 现状（`prepare`）。
- 回滚：删 `authorized_cap` 即恢复现状；代码不内置放宽值。

## 接口和数据改动

- `cli.py release`：`--target`、`--tag`；`--version` 可选（只作比对）；`--confirm` 只放行 `needsUser` 步骤。`cli.py report-failure`：`--stage release --version`。
- 发布执行环境：隔离 `HOME` 与 git 配置；新增 `AUTOPILOT_RELEASE_VERSION`、`AUTOPILOT_RELEASE_TARGET`。
- `release_publish` 入口契约：只在本地建附注 tag `v<AUTOPILOT_RELEASE_VERSION>` 指向 `AUTOPILOT_RELEASE_TARGET`；不推、不建 Release。
- 模板：`tag-and-release` 语义改为本地建 tag（id 不变，仓库已有的 `replace`／`remove` 引用照旧可解析）；引擎内置守卫步 `push-tag`、`await-release`。
- issue 评论块：`autopilot-release-checklist`、`autopilot-release-confirmed`；起始块新增 `target`、`route`。
- `watch-state.json`：`releases`、`released`、`repos[R].lastReleasedSha`。
- `release-limits.json` v2：`release_repos`、`authorized_cap`、`release_creator`。
- 令牌分组：`RELEASE_TAG = {"contents": "write"}`（只用于推 tag）、`RELEASE_READ = {"actions": "read", "contents": "read"}`。

## 风险与回滚

| 风险 | 后果 | 应对 |
|---|---|---|
| App 令牌能推 tag | 令牌泄露可伪造版本 | 只在 `push-tag` 签发；在不跑仓库代码的克隆里只推核对过的对象 id；禁用钩子；用完撤销；六仓 tag 规则集 |
| 仓库命令读到本机凭据 | 越权推送 | 隔离 `HOME`、git 配置与凭据变量；推送不在执行仓库命令的克隆里做 |
| 确认被伪造或引用 | 误放行 | 整条评论恰好等于指令；GraphQL 编辑记录；只认所有者 id；推前重读原评论 |
| 判定读错 `/map --json` | 不该自动的被自动发 | 读不到、类型不对一律按没有授权；没有总体方案的模块从严 |
| 版本号算错 | 版本跳级或漏升主版本 | 只认 F1 格式；主版本独立再核；给了 `--version` 就比对 |
| 中途中断 | 重复推或漏收尾 | 意图与幂等键；推前对账远端；`finishing` 状态独立收尾 |

回滚：删 `authorized_cap` → 全部回到停下确认；`watch --freeze` → 不开始新发版；最坏把 hub 钉回本模块之前的提交。已发出的版本不撤回。

## 验收方式（对应需求成功标准）

| 成功标准 | 怎么验 |
|---|---|
| S1、S2 | 沙盒端到端：一次提前授权的自动发版、一次停下确认的发版（用户原话） |
| S3～S6 | `test_release_gate.py`：T4、主版本、修两次、多模块、人批优先、未归属、需求未做完、合并不在目标里、没有总体方案时从严、取不到开工基线 |
| S4（版本） | 版本计算：`!`、`BREAKING CHANGE:`、`feat`、其余；不合 F1 时停；`--version` 不一致时停 |
| S7、S8 | 确认解析：多行、引用、指纹不符、编辑过（GraphQL）、非所有者、组织仓库；`--confirm` 在 `approve`／推 tag 时被拒；推前重读原评论 |
| S9 | 隔离环境里读不到凭据；tag 引用快照多一条以外的变化被拒；只推核对过的对象 id；远端同名 tag 指向别处时停；恢复时先对账远端 |
| S10 | `await-release`：运行成功且 Release 匹配才继续；运行失败但 Release 已建、没触发、超时都停 |
| S11 | 目标固定、续跑不读主干头、已覆盖、分叉停下、串行与锁、候选排队、首次发版人批、人批交接不把本次 tag 当上次 |
| S12 | 收紧、冻结、降级、主干运行暂停时不开始，进行中的停在下一步前 |
| S13 | 任一步失败时两方都收到通知；30 分钟、24 小时；中断后对账不重做；`finishing` 收尾 |
| S14 | 安装验证失败、主干变红、`report-failure --stage release --version` 都通知并附版本证据 |
| S15 | `test_limits.py`：未授权与现在一致；无 `authorized_cap` 或 v1 时不放宽；`repo_caps` 仍取低；文件非法按 `prepare` |
| S16 | 缺 `release_publish`／`release-tag.yml` 时路线为人批；人批交接从 `await-release` 开始 |
| 交接清单第 5 项 | 五个插件仓库各验一次 App 推 `v*` tag 触发 `release-tag.yml`；旧版 spec-guard（缺 `releasePreauth`）时按无授权停下 |

## 方案取舍

- **版本号由 hub 按 F1 提交类型算，不要求仓库新增输出**：现有仓库没有一个给出升级结论；F1 已在所有仓库强制，`!` 就是不兼容标记，不用改各仓。代价是提交类型写错会算错，所以主版本独立再核、并允许命令行比对。
- **推 tag 放在不跑仓库代码的克隆里，按对象 id 推**：比「同一克隆里加凭据推」多一次本地取对象，但仓库命令再也碰不到写令牌，也改不了要推的对象。
- **守卫步骤由引擎内置**：仓库能改模板步骤，守卫放模板里会被 `remove`／`replace` 绕过。
- **确认以新评论块记录，不改 issue 正文**：现有引擎只信未编辑的正文，改正文会破坏可信判据。
- **watch 只入队、另起进程发版**：避免长步骤卡住 watch 整轮和冻结。
- **上限放宽靠配置值，代码不内置**：回滚只改配置。

## 已定的偏离与外部前提

- **偏离一（交用户批）**：需求说「需求还有模块没完成时不发」。没有总体技术方案的模块，spec-guard 给不出它属于哪个需求；本方案在这种情况下只要能力图上另有已开工未完成的模块，就按停下确认处理，而不是一律不发（一律不发会让无关的长期模块卡住所有发版）。
- **偏离二（交用户批）**：需求文档写「发布引擎的 impact 步骤给出要升主版本」。本方案改为 hub 按 F1 提交类型算版本、独立判主版本，不要求各仓 impact 新增输出，交接清单不加新项。
- 外部前提（需求交接清单已列）：各仓登记 `release_publish`；spec-guard 补 `release-tag.yml`；六仓 tag 规则集。前提不满足的仓库只走人批。

## 审查记录

- 第一轮：Codex 只读审查（delegate review，2026-11-27），24 条阻断、1 条建议，全部核实成立并改入第二稿。其中「`IMPACT:` 输出行没有调研支撑」一条，补查各仓 `interface.yml` 后确认现有仓库都不给升级结论，改用 F1 提交类型计算，不再要求各仓新增输出。

## 变更记录
