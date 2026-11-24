# haigeerlab 插件开发规范（v1.4，2026-11-24 用户批准）

> 本文件是规范的正式存放处（hub 副本），由 autopr-design 维护；改动要用户批准，走 hub 的 PR（T4，由用户合并）。原稿与审查记录在 plugin-ecosystem 总账 `research/2026-11-23-插件开发规范/`。文末附「条目与 hub 检查的对应表」。

> 适用范围：haigeerlab 的六个插件仓库，即 spec-guard-plugin、module-crew、agent-relay、delegate-plugin、kb-plugin、autopilot-hub。业务项目不在范围内。
> 定稿后放在 autopilot-hub 的 `docs/plugin-standard.md`，由 autopr-design 维护，配套检查做成 hub 的共享检查和体检项。规范的任何修改都要用户批准。
> 本规范不重复以下内容：开发流程由 spec-guard 管；会话分工和审批规矩由总账 RULES.md 管。

## 总则

- **两个等级**：
  - **卡**：不满足就拦下，PR 的检查失败，或发版命令拒绝。
  - **提醒**：列在体检报告里，不拦。
- **卡的检查放在哪**：各条卡的检查代码只在 hub 写一份，各仓库调用，不各写一套。每个仓库都必须有 CI（C1），各条卡由 CI 调用 hub 的检查来执行。
- **只管新增**：已有仓库不要求一次改到合规。每条卡在条目里写明「查什么范围」，历史问题只在体检报告里提醒。
- **规则细节以 hub 的检查代码和测试为准**：本文写判定原则和边界。精确的正则、模式清单和测试样例放在 hub 检查代码的测试里，与代码一起改、一起审。两者不一致时，以本文为准修代码。

## A. 安全与隐私

**A1 本机路径和用户名不进仓库（卡）**
- 规则：不得出现真实的本机家目录路径，包括 `/Users/<名>/`、`/home/<名>/`，以及 Claude 项目目录的编码写法 `-Users-<名>-`。在本机运行时，还要按当前登录名不区分大小写、整词再搜一遍。
  - `<名>` 指字母、数字和 `.`、`_`、`-` 组成的一段。
  - 整词指前后不是字母或数字。
- 放行：
  - 只放行一份封闭的占位名单：`me`、`<user>`、`someone`、`example`、`alice`、`bob`、`x`、`u`、`private`、`fixture`。名单写在 hub 检查代码里，增删要经过 PR 审查。
  - 检查代码和测试样例所在的文件，按文件路径逐个列入豁免。
- 查什么范围：PR 里新增或改动的行。新增文件按全文查。
- 在哪拦：PR 的检查；本机提交和推送前。
- 为什么：S107 时，五个公开仓库都带着本机用户名，公开后撤不回来。各家自补的检查有五种写法，覆盖面都不全。

**A2 不提交密钥（卡）**
- 规则：只拦格式明确的密钥。模式清单固定在 hub 检查代码里，带版本号，首版包括：
  - 私钥文件头 `-----BEGIN … PRIVATE KEY-----`；
  - GitHub 令牌：`ghp_`、`gho_`、`ghs_`、`github_pat_` 开头；
  - AWS 访问密钥 `AKIA…`；
  - OpenAI 和 Anthropic 的 API 密钥前缀。
- 放行：只放行豁免名单里的精确字面量，例如 `ghp_EXAMPLE0000…`。一行或一个文件里出现 `EXAMPLE` 字样，不算整体豁免。
- 查什么范围：同 A1。
- 在哪拦：PR 的检查；本机提交前。
- 为什么：机器人私钥就在本机，一旦误提交到公开仓库，即使删掉提交也无法撤回，只能吊销重发。

**A3 LICENSE 统一为 MIT（卡）**
- 规则：仓库根目录要有 `LICENSE`，内容为 MIT 许可证，版权行统一写 `Copyright (c) 2026 haigeerlab`。插件清单写了 `license` 字段的，值为 `MIT`。
- 查什么范围：每次 PR 都查 LICENSE 是否存在、是否为 MIT、版权行是否一致。
- 在哪拦：PR 的检查。
- 为什么：没有许可证的公开仓库，别人按法律不能使用；版权署名不一致，会让人分不清版权归谁。11-24 核查时，kb-plugin、autopilot-hub 没有 LICENSE，其余四个仓库的版权署名有三种写法，其中 spec-guard 署的是个人 GitHub 账号。

## B. 版本与发版

**B1 版本号各处一致（卡）**
- 规则：同一个插件所有已存在的版本声明必须相等。版本声明有三处：
  - Claude 清单 `.claude-plugin/plugin.json` 的 `version`；
  - Codex 清单 `.codex-plugin/plugin.json` 的 `version`；
  - 仓库 marketplace 中同名插件条目的 `version`。
- 两种情况怎么算：
  - 某处没有，例如 delegate 没有 Codex 清单，或 marketplace 条目没写版本，这一处不参与比较。缺清单归 D2 提醒，B1 不因此失败。
  - 同一个 marketplace 里同名条目出现多次，算失败。
- 版本号按原字符串比较，不做规范化。
- 查什么范围：PR 改到任一处版本声明，或新建插件时，比较这个插件的全部版本声明。
- 在哪拦：PR 的检查；发版命令的 check 步骤。
- 为什么：版本号不一致时，Claude 和 Codex 装到的不是同一个版本，排查时还会误以为装的是同一份。

**B2 统一发版命令（提醒；新插件必须有）**
- 规则：
  - 命令为 `scripts/release.py`，子命令 check / prepare / publish / install / smoke / evidence，可以另加 upgrade 等。
  - 每一步最后一行向 stdout 输出一行 JSON，模式名 `<插件>.release/v1`，包含 `status`。`status` 取 ok / preview / refused / waiting-on-user / failed，退出码依次为 0 / 0 / 2 / 3 / 1。日志一律写 stderr。
  - 字段以 spec-guard 的实现为参考样本，hub 定稿后补一份 JSON Schema。
- `--confirm`：publish、install 这类会改外部状态的步骤，不带 `--confirm` 时只预览。带 `--confirm` 时，调用方须持有用户原话并自行留证，例如 autopilot 记在跟踪 issue 里。发版命令本身不核验原话。
- 在 `.github/autopr/interface.yml` 里登记入口，供 autopilot 调用。
- 为什么：现在各家的发版步骤和输出各不相同，autopilot 没法统一调用，也没法统一判断哪一步卡住了。参考实现：spec-guard、agent-relay。

**B3 发版证据统一放（提醒）**
- 规则：`docs/releases/v<版本>-{source,package,claude,codex}.json`。
- 为什么：用来证明「装上的就是发布的那一份」。现在各仓库证据放的位置不同，核对时要逐仓去找。

**B4 CHANGELOG 统一格式（提醒）**
- 规则：采用 Keep a Changelog 格式，带 `[Unreleased]` 段，待发行的说明写在这一段里。
- 为什么：发版命令要从 `[Unreleased]` 取发布说明。delegate 的 prepare 就因为「待发布说明为空」失败过。delegate 目前没有 CHANGELOG。

**B5 标签规则（卡）**
- 规则：
  - 标签名为 `v` 加上与 B1 相同的版本号；
  - 必须是附注标签；
  - 指向的提交必须在 `origin` 默认分支上，判定前先 fetch。
- 查什么范围：只查新建的标签。
- 在哪拦：
  - 有发版命令的仓库，在发版命令里打标签之前拦。
  - 所有仓库再加一道推送标签时的检查，这道检查也调用 hub。标签已经推上去，拦不回来，只能让检查失败并提示删掉重打。在 module-crew、kb-plugin 补齐发版命令之前，这是唯一的检查。
- 为什么：轻量标签和打在未合并提交上的标签，会让「装上的是哪份代码」对不上。agent-relay 的 v0.9.4 就是轻量标签。

## C. 测试与 CI

**C1 每个仓库有 CI，且合并前必须通过（卡）**
- 规则：
  - 有名为 `CI` 的 workflow，PR 和推送主干时都跑；
  - 跑仓库的全量检查（C2 的 `scripts/validate.sh`），以及各条卡的 hub 检查；
  - 在 GitHub 的主干保护里，只把一个汇总任务 `CI required` 设为合并前必须通过的检查。这个任务带 `if: always()`，依赖所有其他任务，任何一个失败或被取消它就失败；它所在的 workflow 不加路径过滤。
- 改主干保护属于改仓库设置，每个仓库都要用户原话同意。
- 查什么范围：hub 体检逐仓检查 workflow 是否存在，以及主干保护是否要求它通过。
- 为什么：没有 CI，「非作者检查」只能靠本机跑，结果没人能复核；有 CI 但不设为必须通过，CI 红了照样能合。只设一个汇总检查，以后加任务、改名都不用改仓库设置；加了路径过滤的必需检查在不相关的 PR 上不会上报，PR 会一直卡住（Codex 的 `blocking-ci.yml`、官方插件市场的 `validate-plugins.yml` 都这么做）。11-24 核查时，module-crew、delegate 没有 CI，kb-plugin、autopilot-hub 有 CI 但不拦合并。

**C2 统一的本地检查入口（提醒）**
- 规则：`scripts/validate.sh`，另有 `--quick` 快档。
- 为什么：CI、Codex 审查和 autopilot 都调用这一个入口，才能保证三方跑的是同一套检查。

**C3 测试里的临时 git 仓库关掉后台维护（提醒）**
- 规则：设 `maintenance.auto=false`、`gc.auto=0`，共用写法由 hub 提供。
- 为什么：maintenance.lock 导致的偶发失败，kb、spec-guard 各踩过一次。

**C4 Python 3.9 兼容，优先只用标准库（提醒）**
- 规则：
  - 有 Python 代码的仓库，CI 至少测 3.9 和一个新版本；
  - 新增第三方依赖要在 PR 里说明理由。按风险分级，新增依赖算 T3。
- 为什么：插件在用户本机直接运行，没有安装依赖这一步；macOS 自带的 Python 版本较老。

**C5 Bash 3.2 兼容（提醒）**
- 为什么：macOS 自带的 bash 是 3.2，用了新语法的脚本在用户机器上会直接报错。

**C7 插件清单与元信息校验（卡）**
- 规则：CI 跑官方的 `claude plugin validate --strict`，校验 marketplace 和插件清单，CLI 版本由 hub 模板统一钉住；再用脚本真正解析所有 skill、command、agent 文件开头的元信息（frontmatter）：YAML 能解析；skill 和 agent 的 `name`、`description` 必填，command 只要求 `description`（command 的名字取自文件名，元信息里没有 `name`）。官方命令不检查 YAML 能否解析，不带 `--strict` 时缺 `description` 也只报警告（kb-lead 11-24 在 2.1.321 和 2.1.328 上实测）。插件代码在仓库根目录的（目前只有 module-crew），先用 `git archive` 导出一份去掉根目录 `CLAUDE.md`、`AGENTS.md` 的副本，再对副本跑 `--strict`：官方命令会把插件根目录下的 CLAUDE.md 判为错误，而这两份是给开发者的规矩，插件运行时不读。Codex 清单暂无官方校验器，按 B1 和 D2 检查。
- 查什么范围：每次 PR 都查全部插件文件。
- 在哪拦：PR 的检查，作为 `CI required` 的一部分。
- 为什么：我们自己写的检查跟不上官方格式的变化；元信息缺字段或 YAML 写错，本地不报错，用户装上才失败。官方插件市场在 CI 里就是这么查的。

**C8 workflow 安全审计（卡）**
- 规则：用 zizmor 审计 `.github/workflows/`，高危发现拦下；新增或改动的 workflow 顶层写 `permissions: {}`，按任务单独放权；`actions/checkout` 设 `persist-credentials: false`，确实需要推送的任务除外，并写明原因。
- 查什么范围：只查本次 PR 新增或改动的 workflow 文件；其余文件的发现列在体检报告里。
- 在哪拦：PR 的检查。
- 为什么：C6 钉 SHA 只防了「引用被挪动」，挡不住权限过宽、危险触发器、凭证留在磁盘上被后续步骤读走。CI 里有机器人私钥。MCP python-sdk 用 zizmor，Codex 和 OpenAI Agents 默认最小权限。

**C9 Dependabot 只升级 Actions，冷却 7 天（提醒）**
- 规则：每个仓库放 `.github/dependabot.yml`，只开 `github-actions`，每月检查一次，`cooldown` 7 天。
- 为什么：C6 钉了 SHA 以后就没人升级了，安全修复也跟不上；冷却 7 天能避开刚发布就被投毒的版本。Codex 就这么配。

**C10 CI 最后检查工作区干净（提醒）**
- 规则：CI 在检查跑完之后单独执行 `git diff --exit-code`，有变化就失败。不放进 `scripts/validate.sh`，因为本机开发时工作区常有未提交的改动，放进去本地会一律失败。
- 为什么：生成文件、版本号或清单改了却漏提交时，本机看起来是好的，别人拉下来就不对。Codex 的 `repo-checks.yml` 最后一步就是这个。

## D. 仓库结构与命名

**D1 插件代码放在 `plugins/<名>/`（提醒，只管新插件）**
- 例外：module-crew 维持在仓库根目录，不迁移。
- 为什么：目录统一后，发版、证据比对和 hub 检查都按同一个路径找清单，不用逐仓写特例。

**D2 Claude 和 Codex 两份清单都要有（提醒）**
- 规则：`.claude-plugin/` 和 `.codex-plugin/` 两份都要有，不依赖 Codex 的自动迁移。
- 为什么：靠自动迁移时，Codex 侧的内容不受版本控制，B1 也比不到。delegate 目前缺 Codex 清单。

**D3 统一命名（提醒）**
- 规则：测试文件统一叫 `test_*.py`。下列共用能力的脚本名以本表为准，新增共用能力时在表里加一行：

  | 能力 | 统一名称 |
  |---|---|
  | 本地全量检查 | `scripts/validate.sh` |
  | 发版命令 | `scripts/release.py` |
  | 路径与密钥检查 | 调用 hub 提供的检查，不在仓库里另放一份 |

- 为什么：名字统一了，autopilot 和人都能按名字找到入口，不用逐仓去问。

## E. 接口与兼容

**E1 `interface.json` 规则（提醒）**
- 版本号是「主.次」两段：只新增时升次版本，破坏性改动升主版本。
- 破坏性改动要先经总负责人评估影响、通知受影响方，再由用户决定。
- 接口声明要和代码对齐，最好有检查，参考 spec-guard 的 `check-interface.py`。
- 为什么：插件之间互相读接口。版本号不说清改了什么，调用方就判断不了能不能继续用。

**E2 依赖别的插件要按接口版本判断（提醒）**
- 读对方的 `interface.json` 判断支不支持某项能力。读不懂、或者两边判断不一致时，按更保守的一方处理：停下或转人批，不默认放行。
- 能直接读对方给出的判定，就不要自己照抄对方的规则。
- 为什么：S106，两个插件各自判断同一件事，结论不一致时偏向了放行。

## F. 提交与 PR

**F1 提交说明用中文，带类型前缀（卡）**
- 规则：只查提交说明的第一行。
  - 格式为 `类型(可选 scope): 说明`，类型限 feat / fix / docs / chore / test / ci / refactor，可以带 `!`。
  - 去掉前缀、反引号里的内容、URL 和路径之后，说明里至少要有一个汉字。判定方式与 hub 的 `rules/chinese_commit.py` 一致。
  - 发版提交也按这个格式写，例如 `chore(release): 发布 v0.94.0`，不另设豁免。
- 放行：
  - 有两个及以上父提交的合并提交；
  - `git revert` 生成的标准格式 `Revert "…"`。
- 查什么范围：PR 相对 merge-base 新增的提交。机器人的提交同样适用。hub 的现有检查还会查 PR 标题和正文，这部分照旧运行，不算本条的卡。
- 在哪拦：PR 的检查，调用 hub 的 `rules/chinese_commit.py`。这个检查现在只查中文，类型前缀的检查要由 autopr-design 补上。
- 为什么：用户读提交历史和 PR 列表要一眼看懂改了什么；类型前缀让发版说明可以按类型归类。

**F2 PR 模板（提醒）**
- 规则：写清改了什么、怎么验证的、有什么风险。参考 spec-guard 的模板。
- 为什么：审查者和 autopilot 都要从 PR 描述里找验证证据，没有固定位置就要翻整段对话。

## G. 文档

**G1 必备文档（提醒）**
- 规则：README（中英两份）、CHANGELOG、SECURITY、LICENSE、CLAUDE.md、AGENTS.md。
- 为什么：README 给用户看；SECURITY 告诉别人怎么报漏洞；CLAUDE.md 和 AGENTS.md 是 Claude 和 Codex 进仓库后读的规矩，缺了就会按默认习惯乱写。

**G2 CLAUDE.md 和 AGENTS.md 共享段一致（提醒）**
- 规则：两份文件里标出的共享段要逐字相同，其余宿主专属的内容可以不同。共享段用 `<!-- shared:begin -->`、`<!-- shared:end -->` 标出。
- 为什么：Claude 和 Codex 在同一个仓库干活，读到的项目规矩不一样，就会各按各的来。

## H. 接入 autopilot

**H1 引用 hub 的版本统一升级（提醒）**
- 规则：各仓库调用 hub 的地方，钉同一个目标基线提交。
  - 目标基线当前是 `713a3d8`。
  - 现状：agent-relay 已在这个基线；kb-plugin 仍是 `3081e7b`；spec-guard、module-crew、delegate 尚未接入 hub 的 workflow。
- 只有 hub 改到以下几处时，才发「需要升钉」的通知，各仓库统一升级：
  - `.github/workflows/`、`admission/`、`degrade/`、`rules/`；
  - 共享检查代码；
  - 体检项的配置格式。
- 为什么：各仓库钉在不同的 hub 版本上，同一条规则在不同仓库会得出不同结果。

**H2 风险配置统一用 `.github/autopr/risk-tiers.yml`（提醒）**
- 规则：`.json` 仍然兼容读取，但新仓库不要再用。
- 为什么：两种格式并存，改风险规则要改两处，容易漏。kb-plugin 目前还是 `.json`。

**H3 CI workflow 统一叫 `CI`（提醒）**
- 为什么：autopilot 的降级流程按 workflow 名字触发，名字不统一就接不上。spec-guard 目前叫 `Validate`，autopilot-hub 叫小写的 `ci`。C1 升为卡后，这一条随 C1 一起检查。

**H4 统一接入 hub 规则（提醒）**
- 规则：各仓库用 `.github/autopr/rules.json` 声明是否启用 hub 的各条规则，没有这个文件就按全部启用处理。
- 为什么：现在没有一个仓库接入 hub 的规则检查，五条卡落地时，需要一个统一的开关位置。

**C6 GitHub Actions 引用钉完整提交 SHA（提醒，只管新增）**
- 规则：新增或改动 `uses:` 时，第三方 action 写完整提交 SHA，后面注释版本号。
- 为什么：标签可以被挪动，钉 SHA 才能保证跑的是审过的那份代码。目前只有 spec-guard 用标签，其余仓库已经用 SHA。

## 落地顺序

1. autopr-design 把本文放进 hub 的 `docs/plugin-standard.md`。
2. 在 hub 写五条卡的共享检查，带合法和违规两类测试样例。
3. **影子运行**：
   - 先只报告、不拦；
   - 用六个仓库的当前主干和最近的 PR 回放一遍；
   - 误报和漏报修完后，交用户看结果。
4. 逐仓开启拦截：
   - 先给 module-crew、delegate 补上 CI（C1）；
   - 各仓开启后，再移除各家原有的同类检查。
5. B5 放进各插件的发版命令，并加上推送标签时的检查。
6. 提醒类条目做成体检项，列在体检报告里，各插件按需补。
7. 各插件在 CLAUDE.md 和 AGENTS.md 里加一行链接，指向本规范。

## 变更记录

- v0.1（2026-11-23）：用户确认需求，逐条同意 5 条卡、20 条提醒，共 25 条。v0.1 正文误写为「24 条」。
- v0.2（2026-11-24）：处理 Codex 审查的 16 条意见。
  - 补齐每条的「为什么」。
  - 写明每条卡查什么范围、怎么放行。
  - 修正条目数量；把 H1 的当前状态改为目标基线。
  - 加入影子运行阶段。
  - 新增 H4、C6 两条，待用户决定。
  - 裁定记录见 `审查裁定-v0.2.md`。
- v1.0（2026-11-24）：用户批准 v0.2 正文，三处都同意（原话「批准，三处都同意」）：F1 取消发版提交豁免；新增 H4、C6。共 27 条：5 条卡、22 条提醒。C6 列在 H 章末尾，编号沿用 C6。
- v1.1（2026-11-24）：用户「对，按推荐改」：C1 升为卡（必须有 CI 且设为合并前必须通过）；A3 升为卡（统一 MIT，版权署名统一写 haigeerlab）；C4 加「CI 至少测 3.9 和一个新版本」。共 27 条：7 条卡、20 条提醒。各插件按 11-24 核查结果发整改单。
- v1.2（2026-11-24）：参考官方与高星仓库（见 `官方高星仓库对照.md`），用户「加这 5 条」：C1 改为只设一个汇总必需检查 `CI required`、不加路径过滤；新增 C7 插件清单与元信息校验（卡）、C8 workflow 安全审计（卡）、C9 Dependabot 冷却（提醒）、C10 工作区干净（提醒）。共 31 条：9 条卡、22 条提醒。C7 按 kb-lead 实测补充：官方命令须带 `--strict`，YAML 另行解析（实施细节，不改条目）。
- v1.3（2026-11-24）：用户「批准，升 v1.3」：C7 只对 skill、agent 要求 `name`，command 只要求 `description`；C10 改为 CI 在检查之后执行，不放进 validate.sh（以上两处来自 agent-relay-owner #22460）；追认 C7 必须带 `--strict` 并另行解析 YAML。条目数不变：31 条，9 条卡。
- v1.4（2026-11-24）：用户「采用导出副本」：C7 补充，插件代码在仓库根目录的，对去掉根目录 CLAUDE.md、AGENTS.md 的导出副本跑 `--strict`（mc-coord #22488 实测冲突）。条目数不变。

## 附：条目与 hub 检查的对应表

> 注意：hub 体检（`checkup/cli.py`）的检查项编号 C1–C14 和本规范的条目编号是两套编号，互不对应。下表左列是规范条目，右列是 hub 里执行它的代码。状态随 hub 的 PR 更新。

| 条目 | 等级 | hub 里怎么查 | 状态 |
| --- | --- | --- | --- |
| A1 本机路径和用户名 | 卡 | `rules/local_paths.py`（PR 新增或改动的行，新文件查全文；本机运行时另查登录名） | 已写，影子运行中（检查 `autopilot/rule/local-paths`，不设为必需） |
| A2 密钥 | 卡 | `rules/no_secrets.py`（模式清单第 1 版，只放行精确假值；不叫 `secrets.py`，免得作为脚本运行时遮住标准库的同名模块） | 已写，影子运行中（检查 `autopilot/rule/no-secrets`，不设为必需） |
| A3 LICENSE 统一 MIT | 卡 | 体检新项（LICENSE 存在、MIT、版权行），并接进 PR 检查 | 待写；hub 自己已满足 |
| B1 版本号一致 | 卡 | `rules/version_consistency.py` | 待写 |
| B5 标签规则 | 卡 | `rules/release_tag.py`（发版命令调用；推送标签时的 workflow 调用） | 待写 |
| C1 有 CI 且汇总检查 `CI required` 为必需 | 卡 | 体检新项（`CI` workflow 存在、无路径过滤，主干保护要求 `CI required`）；各仓 CI 模板带汇总任务 | 待写；hub 自己的汇总任务已加 |
| C7 插件清单与元信息 | 卡 | `claude plugin validate --strict`（模板钉 claude-code 版本；插件在仓库根目录的，对去掉根目录 CLAUDE.md、AGENTS.md 的 git archive 导出副本运行）＋ hub 脚本真正解析 frontmatter YAML：skill、agent 要 `name`、`description`，command 只要 `description`（名字取自文件名） | 待写 |
| C8 workflow 安全审计 | 卡 | 本次 PR 新增或改动的 workflow：zizmor（钉版本）高危拦下；顶层 `permissions: {}`；checkout 设 `persist-credentials: false` | 待写；hub 的 `ci.yml` 已满足后两项 |
| F1 提交说明中文加类型前缀 | 卡 | `rules/chinese_commit.py` | 中文检查已有；类型前缀、合并与 revert 放行待补 |
| 其余 22 条提醒（含 C9、C10） | 提醒 | 体检新项，列在体检报告里 | 待写；hub 自己已加 dependabot（C9） |

各仓库接入方式：在自己的 `CI` workflow 里加一个调用 hub 卡检查的 job（模板由 hub 提供），并由汇总任务 `CI required` 依赖它；按 H1 钉 hub 的目标基线提交；用 `.github/autopr/rules.json` 声明启用哪些规则（H4）。落地先经过影子运行（只报告、不拦），用户看过结果后再逐仓开拦。
