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

### 0. 两条入口并存

- **新入口**（本模块）：`cli.py release --repo R --target SHA` 或 `--tag vX.Y.Z`，以及 watch 触发。只对试点仓库生效：`release-limits.json` v2 的 `release_repos`；文件缺失、读不懂或仍是 v1 时，用代码内置的同一份名单（五个插件仓库与沙盒）识别，并把最终档位压到 `prepare`。下面第 1～11 节的守卫全部适用。
- **旧入口**保留不变：`cli.py release --repo R --version V`、`--resume N`（没有 `target` 的起始块），供站点模板（`frontend` → `site`，`cli.py:928`）和名单外仓库使用；行为与现在一致，不经本模块守卫。名单内的仓库用旧入口新发版时拒绝，提示用新入口。进行中的旧 issue（起始块没有 `target`）按旧入口续跑，用冻结下来的旧插件模板 `pipeline/templates/plugin-v1.yml`（本模块开工前的 `plugin.yml` 原样复制）解析，指纹与起始块比对不变。
- **仓库名规范化**：入口先读 `GET repos/R` 的 `full_name`，锁文件、状态键、意图键都用它的 `casefold()`；读现有 `watch-state.json` 时，按 `casefold()` 匹配已有键（兼容大小写不同的旧键），写回时迁移成规范键。

### 1. 资格与目标提交

- 资格（新入口统一核）：仓库在 `release_repos`、不是 hub、`owner.type == "User"`；`target` 在默认分支历史上；`target` 上**主干 push 运行全部成功**：必须出现的工作流 = 降级 caller 里 `workflow_run.workflows` 列出的主 CI（例如 `CI`），再加上 `target` 上实际出现的其他 push 运行；每个工作流取 `target` 上最新一次运行的最新 attempt，全部 `completed` 且 `success`；另外，主干规则要求的必需检查（`flow.verified_requirements` 读出的集合）里，除 `autopilot/verified` 外（它写在 PR head 上），每一项在 `target` 上都要有成功结果（check run 或 commit status 皆可）。任一不满足 → 停，不开 issue。PR 有没有可信证据不在资格里判，缺证据的按「未归属」进第 4 节。
- `resolve_pipeline`、档位、守卫全部基于 `target`；起始块记 `target`，续跑只用起始块的值。

### 2. 发布范围、模块归属与需求分组（`release_gate.py`）

- **版本 tag**：只认 `^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`；解析失败、查询失败 → 停。
- **上次发版提交**（新发版与人批交接共用）：`state.repos[R].lastReleasedSha`；没有时取 `target` 的严格祖先上、版本号最大的合规版本 tag 的解引用提交（指向 `target` 本身的 tag 只有在 `released` 里有记录时才算已发）；一个都没有 → 首次发版，`route = human`。
- **先后**：`target` 是上次的后继 → 继续；是祖先或相等 → `covered`；分叉 → 停，记暂停交用户。
- **范围内的 PR**：`git rev-list 上次..target`；每个提交用 `GET commits/{sha}/pulls` 找 PR，要求已合并、`merge_commit_sha` 在范围内（不限 PR 的目标分支，module-crew 先合进集成分支、再由 batch PR 带进主干的内层 PR 也算）；模块取机器人发、绑定 PR head、未编辑的最新 v2 证据块（复用 `cli.py:1236-1248`）的 `module`。找不到合格 PR 或证据的提交 → 「未归属」。
- **需求分组**：在 `target` 上跑 `map_view.py --json`。有 `overallDesign.path` 的模块按它分组，组内模块须全部 `status: done`，且每个成员在 `target` 历史上至少找到一个带它的可信 v2 证据、证据里有该模块合并前验证报告（`verification`）的已合并 PR（只改 spec／tasks 的 PR 没有验证报告，不算实现落地）；有成员找不到 → 这次不发（记「需求未做完」，等下一轮），与需求一致。**没有总体方案的模块**：能力图上另有未完成的模块（已开工或未开工）时，`route` 至少 `confirm`，清单里列出这些模块的 id、职责、状态和最近的 PR，由用户确认时判断是否同一需求；用户确认即表示「这些不属于本次的需求」（偏离一）。
- **入口可用性**：`target` 上解析出的流水线里，凡 `entry.py <名字>` 形式的 AI 步骤，其入口都要在 `interface.yml` 登记（`impact` 除外，见第 3 节）；`tag-and-release` 必须存在、没被仓库 `replace`、仍在 `at: release`，引擎把 `push-tag`、`await-release` 紧接着插在它后面，`install-local` 及之后的步骤都排在 `await-release` 之后（解析后的顺序不满足就判 human）；`.github/workflows/release-tag.yml` 存在；`release_creator[R] != "local"`；仓库有生效的 tag 规则集，`refs/tags/v*` 的创建只放行 hub App 与用户本人（规则集绕过名单里只有这两个身份；读 `GET repos/R/rulesets?targets=tag` 及各规则集详情；读不到按没有）。任一不满足 → `route = human`，原因写进汇报。

### 3. 版本号（hub 按 F1 计算）

- 版本号由 hub 算：上次版本号，加上范围内**每个已合并 PR 的标题**的类型：有 `!` 或 PR 正文含 `BREAKING CHANGE:` → 主版本 +1；否则有 `feat` → 次版本 +1；否则修订号 +1。用 PR 标题而不是逐个提交，避开 merge commit 和 PR 内部的中间提交。PR 标题不合 F1（例如 hub 自己开的收窄 PR 用了全角冒号）→ 这个 PR 按修订号计，`route` 至少 `confirm`，清单列出这些 PR 标题，由用户确认版本号。
- 是否升主版本 = 新主号 > 上次主号。提交类型由作者写，标错时算不准，这一点在「风险」里列为残余风险；T4 改动另由作废条件拦住。
- 模板的 `impact` 步改为只记证据：入口没登记（退出码 3）记「未登记，跳过」，不停；登记了但退出非 0 → 停。
- 命令行给了 `--version` 时与算出的比对，不一致就停。人批交接的版本取自 tag 名。

### 4. 路线判定与最终档位

- `route` 先后：任一模块 `takeover.release == human`、或第 2 节判为 human → `human`；否则任一模块 `takeover.release` 低于 `auto-confirm`、`releasePreauth is not True`、作废（`module_scope.py` 的 `scope_base` 取不到或不是 `target` 祖先、`scope_base..target` 含 T4、升主版本）、有未归属、或第 2 节判为至少 `confirm` → `confirm`；否则 `auto`。
- **最终档位**（唯一函数）`final_level(route, phase, confirmed, raw, pipeline, limits)`：
  - `auto`，或 `confirm` / 人批交接**已确认** → `effective_level("auto-confirm", ..., authorized=True)`（以 `auto-confirm` 为期望值再与上限取低）；
  - `confirm` / 人批交接**未确认** → `min("prepare", effective_level(raw, ...))`（显式的 `human` 上限照样压住）；
  - `human` 交接前 → `human`。
  - 人批交接不把远端 tag 当授权：交接后 hub 照样发清单，等仓库所有者在 issue 回复 `确认发版 <指纹>`，确认后才按上面第一条跑安装、验证、记录。
  - 试点仓库读不到 `release-limits.json` 或它不是合法的 v2：新入口照样走，但所有路线的最终档位都不超过 `prepare`，汇报写明。
- 最终档位低于这一步需要的档位 → 这一步按现有 `decision` 等待，清单把它标为 `needsUser`，原因写明（例如「上限文件压到 prepare」）。推 tag 守卫再算一次。

### 5. 可信清单与确认

- **每次发版开始时都发清单**（自动路线也发，作为续跑依据）：机器人追加评论块 `autopilot-release-checklist {seq, target, version, route, reasons, modules, steps:[{id, at, by, run, needsUser}], fingerprint}`；`needsUser` 按**确认之后**要用的档位算（`confirm` 路线按确认后的 `auto-confirm`，`auto` 路线就是本次档位），所以用户确认的就是之后实际执行的那份清单；每步记解析后的实际命令、证据要求和 `stop_if`。`fingerprint = sha256(规范化 JSON(target, version, steps))`。续跑时只认机器人发的、未编辑的清单和确认块，命令与清单不一致就停。清单有变就发 `seq+1`，旧确认作废。
- **确认判定**：作者 `user.id == 仓库所有者 id`、`user.type == "User"`；GraphQL `IssueComment.lastEditedAt` 为空；评论 id 大于最新清单评论；正文按 Markdown 逐行看，忽略围栏代码块内、四空格或制表符缩进、以 `>` 开头的行，其余行里**恰有一行**去掉首尾空白后等于 `确认发版 <指纹前 8 位>`，且这一行是正文第一行或前一行是空行（排除 Markdown 引用的无 `>` 续行）；其他行可以是普通说明。GraphQL 字段在沙盒端到端验收时实读核对。
- 确认成立 → 机器人追加块 `autopilot-release-confirmed {seq, fingerprint, commentId}`。
- 暂停后继续：所有者按同样规则回复 `继续发版 <seq>`，或修好后由命令行续跑、该步重跑通过（重新验证）。`--confirm` 只放行当前等待且 `needsUser` 的步骤；当前等待的是 `approve` 或推 tag 时拒绝。`watch --resume R` 解除仓库收紧，并同时清掉仓库级巡检出错暂停（顺带修掉现在只清 `narrowed` 的缺口，`state.py:182-188`）；它不解除发版暂停。

### 6. 本地建 tag 与推 tag

- **先对账远端**：`git ls-remote origin refs/tags/v<版本> refs/tags/v<版本>^{}`：已有且解引用等于 `target` → 跳过建和推，补记 `released`（对象 id 取远端值）后到第 7 节；已有但不等 → 停；没有 → 往下。远端没有时，本地建 tag 的结果不算数，丢了就重建（还没发布，重建不影响）。
- **`tag-and-release`（模板步，本地建 tag）**：在**隔离克隆 A** 里执行 `entry.py release_publish`。环境：临时 `HOME`；`GIT_CONFIG_NOSYSTEM=1`、`GIT_CONFIG_GLOBAL=/dev/null`、`GIT_TERMINAL_PROMPT=0`、`GIT_ASKPASS=/bin/false`、`SSH_ASKPASS=/bin/false`；去掉 `SSH_AUTH_SOCK` 和 `GH_*`、`GITHUB_*`、`GIT_*` 等凭据类变量；另给 `AUTOPILOT_RELEASE_VERSION`、`AUTOPILOT_RELEASE_TARGET`（保留 `AUTOPILOT_VERSION`）。前后各记 `git for-each-ref refs/tags --format='%(refname) %(objectname)'`：只允许新增 `refs/tags/v<版本>` 一条，其余不增不改；新对象 `git cat-file -t` 为 `tag`、`^{commit} == target`。
- **`push-tag`（引擎内置守卫步）**：重新核授权（两条路线都在 `target` 上重算第 2、4 节，含 tag 规则集；`confirm` 另外重读最新清单与确认原评论）；在 hub 自己的**克隆 B**（全新、不执行仓库代码）里只取那一个 tag 对象（`git fetch <A> <对象 id>`），核对 id；用令牌 `RELEASE_TAG = {"contents": "write"}` 执行 `git -c core.hooksPath=/dev/null push --no-verify origin <对象 id>:refs/tags/v<版本>`；推后 `ls-remote` 读回核对；令牌用完撤销；写 `released[R][version] = {target, tagObject, pushedBy: "hub", pushedAt}`。
- **隔离边界**：`tag-and-release` 的环境隔离是尽力而为（去掉凭据变量和 git 配置），命令仍以本机用户身份运行，理论上能用到本机其他凭据；真正的保证在推送一侧：hub 只在克隆 B 里按核对过的对象 id 推，仓库命令碰不到 App 写令牌。只有 `tag-and-release` 做这层隔离；`prepare`、`install-local`、`verify-install`、`record` 照现状执行，因为它们要用用户本机环境安装插件。它们拿不到 App 写令牌（令牌只在 `push-tag` 的进程里签发），但能读到用户本机已有的登录凭据，这是现状就有的残余风险，由 tag 规则集与「会话不自己打 tag」规则兜住，列入风险。

### 7. 等 Release 就绪

- `await-release`：令牌 `RELEASE_READ = {"actions": "read", "contents": "read"}`。`GET repos/R/actions/workflows/release-tag.yml/runs?event=push` 里取 `head_branch == "v<版本>"`、`head_sha == target`的运行（hub 推的 tag 另要求创建时间不早于 `released[R][version].pushedAt`，这个时间在 `push-tag` 时持久记下，恢复时从这里读；人批交接不按时间筛，因为交接前的那次运行正是要核对的），按 `run_number` 取最新，再取它最新的 `run_attempt`；记下 `runId`、`attempt`。每 30 秒查一次，单步总时限 30 分钟：运行 `completed` 且 `success`；`GET releases/tags/v<版本>` 存在、`tag_name` 一致、名字含版本号；远端 tag 解引用等于 `target`。运行失败（即使 Release 已建）、没出现、超时 → 停。通过后 `released[R][version]` 补 `releaseUrl`、`runId`、`attempt`，并置 `ready: true`；只有 `ready` 的记录才算「已发版本」。
- `release_creator[R] == "local"`：只走人批；交接后只核 Release 存在、对应 tag，记 `released`（`runId` 为空）。

### 8. 状态、锁、认领与暂停（4.6）

- **锁**：每仓库文件锁 `~/.autopilot/release-<规范名>.lock`，命令行和 watch 的认领都先拿它。
- **认领**：在锁内读 `releases[R]`：`state` 为 `running` 且记录的进程还活着、或为 `waiting-human`、`paused`、`finishing` → 不认领新候选（`finishing` 和 `running` 进程已死时只续跑原发版）；只有 `state` 为空、`done` 或 `covered` 时才把选中的候选写成 `{state: running, target, pid, since}`，持久化后释放锁，再执行步骤（长步骤不持锁；每步开始前重新拿锁核对自己仍是认领者）。
- **候选**：watch 在主干运行变绿且过资格时把 `target` 追加进 `releases[R].candidates`（去重），持久化后才推进游标；在 watch 整轮锁之外，对有候选且未认领的仓库启动 `cli.py release --repo R --target <最新候选>` 子进程。**选候选**：在候选里选一个是其他所有候选后继的提交（`git merge-base --is-ancestor` 核对）；是它祖先的候选在它发完后标 `covered`；互不包含的候选（不该在主干上出现）保留并在汇报里写明。最新候选因需求未做完不能发时，保留在队里，下一个更新的候选到来时替换它。
- **等确认与暂停的恢复**：watch 每轮查 `waiting-human` 和 `paused` 的发版 issue，发现新的合格确认或 `继续发版` 后启动续跑子进程（超时暂停、失败暂停都适用）。
- **记录**：`releases[R] = {state, target, version, route, issue, step, since, pid, pause, candidates}`；`state` 为 `planned|running|waiting-human|finishing|done|paused|covered`。`released[R][version]` 见第 6、7 节；人批交接也建这一条。
- **幂等**：意图键 `<规范名>:release:<target>:<version>:<步骤>:<动作>[:<对象>]`，先记后做、做完写 done。中断后：tag 看远端；issue 与评论按块里的键查；`install-local`、`verify-install`、`record` 这三步执行中断（有意图、无完成记录）时，不自动重跑：发版进入 `paused`，清单评论写明中断在哪一步、之前已完成哪些步；用户看过本机状态后回复 `继续发版 <seq> 从 <步骤 id>`（只能是中断那步或它之后的一步），hub 把之前的步骤按用户声明记为已完成（原话进块），从指定步骤继续；`record` 完成后写账本键 `release_record:<规范名>:<version>`。
- **守卫**（开始前和每步之前）：`frozen`；`repos[R].narrowed`；该仓库主干运行记录里，**任一工作流**有 `fields.pause` 或红色分类，而该工作流之后在它的后继提交上还没有一次成功运行（按工作流分别判，另一条工作流的成功不能解除它）；`repos[R].pause`；未关闭的降级 issue。任一成立 → `paused`，记原因。
- **失败**：任一步 `stopped` → `paused`，通知用户（桌面 + issue 评论）和负责的会话；时限：单步 30 分钟，`waiting-human` 24 小时 → `paused`。
- **收尾**：Release 就绪、安装与验证完成后进入 `finishing`，依次做（各带意图键）：写 `lastReleasedSha = target`、`released` 完成标记、账本 `release_done`、总结评论、关 issue，最后 `done`。`finishing` 由状态驱动续跑，不经 issue 的起始块校验（已关闭的 issue 不影响），每一项按意图键对账，不做覆盖判定。

### 9. 人批交接

- `cli.py release --repo R --tag vX.Y.Z`：读远端 tag 解引用提交作为 `target`，过第 1 节资格（不要求提前授权），`route = human`；发清单、等所有者确认后按第 4 节跑；跳过建／推 tag，从 `await-release`（或 `local` 仓库的 Release 核对）开始；上次发版提交按第 2 节同一规则取（先 `lastReleasedSha`，没有再取这个 tag 之前最近的合规版本 tag）。缺安装、验证、记录入口时，这几步标 `needsUser`，由用户本人做完后在 issue 回复或命令行 `--confirm`。

### 10. 汇报与通知

- **开始前**：账本写「将要做什么」：仓库、目标、版本、路线、清单、每个模块的授权与作废事实、范围内 PR 的修复次数（`state.repairs`）。通知负责的会话：范围内 PR 在 `state.prs` 的 `notify` 去重；没有就只通知用户。
- **发版后发现问题**：`verify-install` 失败 → 暂停并通知，附 `released[R][version]` 的 tag、Release、运行、issue；发版后主干 CI 变红 → 取 `released[R]` 里 `ready` 为真、`target` 是这次红色运行 `headSha` 的祖先或相等、且最新的那一条，作为「涉及的已发版本」写进 watch 的通知；`cli.py report-failure --repo R --stage release --version vX.Y.Z --detail …`：按版本查 `released` 补齐证据后通知用户（`--stage release` 时 `--pr` 可省）。

### 11. 上限（T4，最后一个实现 PR）

- `effective_level(repo, raw, pipeline, limits, *, authorized=False)`：只有 `authorized` 为真**且** `release-limits.json` 为 v2 并给出 `authorized_cap`（只能是 `auto-confirm`）时，`pipeline_own` 的上限改取 `authorized_cap`，再与 `repo_caps` 取低；其余情况与现在完全一样（非 `pipeline_own` 的仓库照现有规则算，kb 的 `repo_caps` 本来就是 `auto-confirm`）。文件缺失、读不懂、v1 → 现状。
- 回滚：删 `authorized_cap` → 四个流水线自用插件回到最高 `prepare`（kb 的 `repo_caps` 本来就是 `auto-confirm`，不受影响）；要让所有仓库都停下确认，把 `release-limits.json` 改回 v1 或用 `watch --freeze`，或把模块 `takeover.release` 改回 human。

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
| 其他发布步骤读到用户本机凭据（现状就有） | 仓库命令可用用户身份推 tag | tag 规则集只放行 App 与管理员；规则「会话不自己打 tag」；残余风险，第一版接受 |
| PR 类型标错（`!` 漏写） | 该升主版本没升，提前授权没作废 | T4 由作废条件另拦；残余风险，第一版接受，汇报列出范围内 PR 标题 |

回滚：删 `authorized_cap` → 四个流水线自用插件回到停下确认（kb 不变）；改回 v1 → 所有试点仓库最高 `prepare`；`watch --freeze` → 不开始新发版；最坏把 hub 钉回本模块之前的提交。已发出的版本不撤回。

## 验收方式（对应需求成功标准）

| 成功标准 | 怎么验 |
|---|---|
| S1、S2 | 沙盒端到端：一次提前授权的自动发版、一次停下确认的发版（用户原话） |
| S3～S6 | `test_release_gate.py`：T4、主版本、修两次、多模块、人批优先、未归属、需求未做完、合并不在目标里、没有总体方案时从严、取不到开工基线 |
| S4（版本） | 版本计算：`!`、`BREAKING CHANGE:`、`feat`、其余；不合 F1 时停；`--version` 不一致时停 |
| S7、S8 | 确认解析：附说明的有效确认、引用（含无 `>` 续行）、代码块、指纹不符、编辑过（GraphQL）、非所有者、组织仓库；`--confirm` 在 `approve`／推 tag 时被拒；推前重读原评论并重核规则集；确认后执行的步骤与清单一致 |
| S9 | 隔离环境去掉了凭据变量与 git 配置（同用户的其他凭据是已接受的残余风险）；tag 引用快照多一条以外的变化被拒；只推核对过的对象 id；远端同名 tag 指向别处时停；恢复时先对账远端 |
| S10 | `await-release`：运行成功且 Release 匹配才继续；运行失败但 Release 已建、没触发、超时都停 |
| S11 | 目标固定、续跑不读主干头、已覆盖、分叉停下、串行与锁、候选排队、首次发版人批、人批交接不把本次 tag 当上次 |
| S12 | 收紧、冻结、降级、主干运行暂停时不开始，进行中的停在下一步前 |
| S13 | 任一步失败时两方都收到通知；30 分钟、24 小时；中断后对账不重做；`finishing` 收尾 |
| S14 | 安装验证失败、主干变红、`report-failure --stage release --version` 都通知并附版本证据 |
| S15 | `test_limits.py`：未授权与现在一致；无 `authorized_cap` 或 v1 时不放宽；`repo_caps` 仍取低；文件非法按 `prepare` |
| S16 | 缺 `release_publish`／`release-tag.yml` 时路线为人批；人批交接从 `await-release` 开始 |
| 交接清单第 5 项 | 五个插件仓库各验一次：App 推 `v*` tag 触发 `release-tag.yml` 并实际建出对应 Release；旧版 spec-guard（缺 `releasePreauth`）时按无授权停下；GraphQL 评论编辑字段实读；用 hub 的令牌实读各仓 tag 规则集与绕过名单 |
| 入口并存、规范化、候选 | 旧入口（站点）行为不变；名单内仓库用旧入口被拒；大小写不同的仓库名共用一把锁；总选最新候选、不完整候选被更新的替换；认领与进程存活 |
| 人批交接 | 交接后安装、验证、记录按 `auto-confirm` 跑；缺入口时标 `needsUser` |

## 方案取舍

- **版本号由 hub 按 F1 提交类型算，不要求仓库新增输出**：现有仓库没有一个给出升级结论；F1 已在所有仓库强制，`!` 就是不兼容标记，不用改各仓。代价是提交类型写错会算错，所以主版本独立再核、并允许命令行比对。
- **推 tag 放在不跑仓库代码的克隆里，按对象 id 推**：比「同一克隆里加凭据推」多一次本地取对象，但仓库命令再也碰不到写令牌，也改不了要推的对象。
- **守卫步骤由引擎内置**：仓库能改模板步骤，守卫放模板里会被 `remove`／`replace` 绕过。
- **确认以新评论块记录，不改 issue 正文**：现有引擎只信未编辑的正文，改正文会破坏可信判据。
- **watch 只入队、另起进程发版**：避免长步骤卡住 watch 整轮和冻结。
- **上限放宽靠配置值，代码不内置**：回滚只改配置。

## 已定的偏离与外部前提

- **偏离一（交用户批）**：需求说「需求还有模块没完成时不发」。没有总体技术方案的模块，spec-guard 给不出它属于哪个需求；本方案在这种情况下只要能力图上另有未完成的模块（已开工或未开工），就停下确认，清单列出这些模块，由用户确认时判断是否同一需求，而不是一律不发（一律不发会让无关的长期模块卡住所有发版）。
- **偏离二（交用户批）**：需求文档写「由 impact 结论算出版本号」。本方案改为 hub 按范围内已合并 PR 标题的 F1 类型算版本，不要求各仓 impact 新增输出，交接清单不加新项；impact 步只记证据。
- 外部前提（需求交接清单已列）：各仓登记 `release_publish`；spec-guard 补 `release-tag.yml`；六仓 tag 规则集。
- **新增的外部前提（交接清单要补，随本方案交用户批）**：插件模板的 `full-test`、`prepare`、`install-local`、`verify-install`、`record` 也经 `entry.py` 调仓库入口，现在只有 delegate 登记了 `release_prepare`、`check`，其余仓库都没登记。各仓要把 `check`、`release_prepare`、`install_local`、`verify_install`、`release_record` 也登记齐（或在 `pipeline.yml` 里 `replace`／`remove` 掉不需要的步骤），否则按第 2 节只能走人批。
- 前提不满足的仓库只走人批（第 2 节逐项核对）。

## 审查记录

- 第一轮：Codex 只读审查（delegate review，2026-11-27），24 条阻断、1 条建议，全部核实成立并改入第二稿。其中「`IMPACT:` 输出行没有调研支撑」一条，补查各仓 `interface.yml` 后确认现有仓库都不给升级结论，改用 F1 提交类型计算，不再要求各仓新增输出。
- 第二轮：Codex 复审（带第一轮报告），上一轮 7 条已解决、17 条仍有缺口，另有 7 条新阻断，全部核实成立并改入第三稿：新旧入口并存与仓库名规范化；资格改看主干 push CI；发布范围不限 PR 目标分支；版本 tag 严格匹配；版本按 PR 标题算、impact 只记证据；入口、模板替换与 tag 规则集都进路线判定；确认按 Markdown 逐行、允许附说明；人批交接后档位；每次都发清单；认领、总选最新候选与等确认的恢复；安装与记录的对账；收尾逐项对账；已发记录补运行证据与主干红的归属；隔离边界与残余风险写明。
- 第三轮：Codex 复审（带第二轮报告），上一轮 8 条已解决、16 条仍有缺口，另有 5 条新阻断，全部核实成立并改入第四稿：清单按确认后的档位算、记实际命令；人批交接不把远端 tag 当授权、照样等确认；资格的主干 CI 集合以降级 caller 的主 CI 为准；旧主干记录被后继成功运行解除；候选按祖先关系选；认领不覆盖等待、暂停、收尾中的发版；本机三步中断改为暂停等用户确认；已发记录分 `ready`；最终档位以 `auto-confirm` 为期望值、v2 缺失时最高 `prepare`；规则集只认 App 与用户本人、两条路线推前都重核；PR 标题不合 F1 时转确认；确认行须为段首；隔离边界改写为尽力而为。
- 三轮审查用的是默认标准（severity-v1），spec-guard 记技术方案审查要 design-v1，所以第四稿另跑一轮 `--profile design` 作审查记录。
- 第四轮：Codex 按设计标准（design-v1）审第四稿，6 条阻断、9 条建议；阻断全部改入第五稿（必需检查、成员落地证据缺失时不发、暂停按工作流判、暂停后也读继续指令、人批运行不按时间筛并持久记推送时间、本机步骤中断后由用户声明从哪步继续），建议采纳 8 条（入口登记补进交接清单、旧插件模板冻结、状态键兼容、`watch --resume` 清巡检暂停、无 v2 时的试点识别、验收措辞、规则集读取验收、偏离一给出的信息）。这一轮报告缺「同厂商」字段（未带 `--author-host`），不能记作 spec-guard 审查记录。

## 变更记录
