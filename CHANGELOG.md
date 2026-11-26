# Changelog

本文件按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式记录 autopilot-hub 的变更。

hub 不发版本号：各仓库按 40 位提交 SHA 引用 hub，「需要升钉」的变更会在条目里注明（插件开发规范 H1）。待发布或尚未通知升钉的变更写在 `[Unreleased]`。

## [Unreleased]

### Fixed
- 体检规范 C3：测试目录里的二进制文件（开头 8 KiB 有 NUL 字节，不论大小）直接跳过，不再让整项变成「判断不了」；带 BOM 的 UTF-16、超过 1 MiB 或不是 UTF-8 的文本仍算读不了。
- 规范 A1/A2 的本机 diff 模式：二进制文件的改动（含改名、带空格或非 ASCII 的文件名）不再被悄悄丢掉，和 GitHub 接口模式一样列为「没法检查」；删除二进制文件不列；文件名解析不出来时列原始 diff 头。
- `scripts/validate.sh --quick`：测试或模块把子目录加进搜索路径、再按名字导入时也能选中（按路径后缀补依赖，宁可多选）。hub 自己的选择结果不变。

### Removed
- 删掉 `correction/cli.py` 里没人用的 `CANDIDATE_BLOCK` 正则（候选块照常写入）。`pipeline/engine.py` 写进本机账本的 `release_done` 是切片 14 Spec 定的发布完成记录，保留并在代码里注明暂无读取方。不需要升钉。

### Added
- S110 切片 S5 剩余：合规监测对已开工的 spec-guard 新流程模块查三项（`audit/flow_check.py`）——需求没有经你确认（严重；确认后又改过为提醒）、重要需求的技术方案不是你批的（严重）、模块已完成却没有成立的合并前验证记录（严重；验证记录只在你本机检出的公共 git 目录里，从 `~/.autopilot/config.json` 的 `checkouts` 读，没配检出时只写「判不了」）。老流程模块仍由 `audit/plan_check.py` 检查（方案原写退役，因老模块仍在用而保留）。不需要升钉。
- S110 切片 S4：新流程模块合并后，把合并前验证报告里的每条意见开成后续 issue（标签 `autopilot:followup`，标题「[跟进] 模块：意见」，带来源 PR 与补丁指纹）；同模块同标题已有机器人开的 issue 只追加评论，同一 PR 同一补丁重跑一律跳过。开失败不回滚合并，在 PR 上留补开命令 `orchestrator/cli.py followups --repo <owner/name> --pr <n>`（只认机器人 v2 证据与 sha256 对得上的本机报告）。不需要升钉。
- S110 切片 S3：新流程模块的证据评论 `autopilot-evidence:v2`（补丁指纹、merge-base、模块、检查、合并前验证结论与报告／回归文档 sha256、已核对产物）；硬卡 `admission/gate.py` 同时认 v1 与 v2，v2 要求结论为 pass／pass-with-notes、模块与接管评论一致、仍绑定当前 head（工作流不重算补丁指纹，rebase 后由编排复用验证结论、重发绑定新 head 的证据）。编排在合并前验证成立后发 v2 证据、过本地准入并合并。切片 12 改档对新流程模块改为：需求由用户确认，且技术方案已批（用户或审查方）、不需要审或没有技术方案。新流程模块的接管评论带 `flow: v2`，硬卡据此只认 v2 证据，其他模块只认 v1。检查输出和意见标题写进机器人评论前转义 HTML 注释标记。**需要升钉**（改到 `admission/`）：本机编排用 hub 自己的准入代码判定，升钉前也照样自动合并；没升钉的仓库，远端准入检查（只报告、不拦）读不懂 v2，会显示「不会自动合并」。
- S110 切片 S2 合并前验证（⑦）：spec-guard 新流程模块（`tier` 不为 null）由本机编排照回归文档执行用例、写 `spec-guard.verification/v1` 报告，交 spec-guard 0.96.0 校验并记录（`verify/core.py`、`orchestrator/flow.py` 的 `verify_module`）。与基础分支一致的命令用例自动执行，新增或改过的命令要你在跟踪 issue（`autopilot:verify`）回复「确认执行」后才跑；人工与 ai-operate 用例由你在跟踪 issue 回复结果。验证成立并记录后停下交你合，证据评论 v2 与硬卡在 S3 接上。老流程模块仍走 Codex 最终验收。不需要升钉（只改本机编排）。
- `docs/plugin-standard.md`：《插件开发规范》正式存放处（现为 v1.7.3），附条目与 hub 检查的对应表。
- `.github/dependabot.yml`：只升级 GitHub Actions，每月一次，冷却 7 天（规范 C9）。
- `LICENSE`（MIT）、`.gitignore`、`CLAUDE.md`、`AGENTS.md`、`README.en.md`、本文件。
- 合规监测（切片 18a–18d）：`audit/cli.py`、批准基线、合规周报 issue 与违规 issue、按审查轮次核验 Codex 批准。

- 规范卡 A1、A2 的共享检查：`rules/local_paths.py`、`rules/no_secrets.py`（共用 `rules/diffs.py`），接进可复用的 `rules.yml`，影子运行期只报告。**需要升钉**（改到 `rules/` 与 `.github/workflows/`）。

- 规范卡 B1、B5、F1 的共享检查：`rules/version_consistency.py`、`rules/release_tag.py`、`rules/commit_format.py`；B1、F1 接进 `rules.yml` 影子运行，B5 供发版命令和推送标签的 workflow 调用。**需要升钉**。

- 规范卡 C7、C8 的共享检查：`rules/frontmatter.py`（元信息真正解析 YAML）、`rules/workflow_audit.py`（改动的 workflow 最小权限与不留凭证）、`rules.yml` 跑 zizmor 1.32.0；`rules.yml` 新增输入 `mode: report|enforce`。各仓模板 `examples/caller-ci-standard.yml`（含 `CI required`、C7 的 `claude plugin validate --strict`）、`examples/caller-release-tag.yml`（B5）。**需要升钉**。

- 体检报告加规范项（`checkup/standard.py`，编号用「规范」前缀，和体检原有的 C1–C14 区分）：规范A3、规范C1、规范C9、规范C10。

- 影子运行后的修复：hub 的可复用 workflow 与示例调用文件顶层改为 `permissions: {}`、权限移到任务级（范围不变）；降级调用的 `workflow_run` 加 zizmor `dangerous-triggers` 行内忽略并写明原因；新增 0 行的空文件不再判为"没法检查"。**需要升钉**（改到 `.github/workflows/`、`rules/`）。

- 修正：PR 没改 workflow 时，`rules.yml` 的 zizmor 一步曾不带输入运行、以退出码 2 让 `rules` 任务失败（各仓换用新模板后会拦下所有不改 workflow 的 PR）。`workflow_audit.py --list-changed` 无文件时不再输出空行，`rules.yml` 改用数组判断。**需要升钉**。

- F1 与中文检查放行 Dependabot（用户定）：PR 作者和提交作者都是 `dependabot[bot]` 的提交放行 F1；PR 作者是 `dependabot[bot]` 时中文检查放行。按 GitHub 返回的账号判断，不看文字。**需要升钉**。

- 体检报告补齐规范提醒类条目（`checkup/standard.py`，共 22 个规范项）；`standard-target.json` 记规范 H1 的目标基线。
- 模板 `caller-ci-standard.yml`：PR 改标题、正文（`edited`）时整套 CI 重跑，并加 `concurrency`。

- 合规监测的 Plan 审批检查跳过新流程模块（`tier` 不为空）：spec-guard 1.21 起它们的 Plan 写完即自动批准，原检查会误报"Plan 未批就开工"。

- `scripts/validate.sh`（规范 C2）：全量检查入口，按 `unittest discover` 的规则逐个跑测试文件并打印最慢 5 个；`--quick` 只跑改动涉及的测试（按导入关系追到间接依赖，对应不上退回全量，有文档改动时加跑扫全仓的测试）。CI 的测试步骤改为调用它。

### Changed
- 非 spec-guard 仓库一律交你合（S110 方案 Q-A1：没有回归文档就不自动合并），本机编排删掉了只服务这条路径的 delegate 代码审查。
- S110 切片 S1 与 S5 配置部分（跟随 spec-guard 接口 1.22 的接管档位改版）：
  - 编排：spec-guard 项目的合并档位改为在基础分支提交上跑 `map_view --json`，读模块生效的 `takeover.merge`；读不到、spec-guard 早于 1.22（没有 `designForcedBy`）、字段缺失，一律按人合。删掉自己解析 `.agent/config.json` 的那套算法；收窄说明改为「合并 → 技术方案 → 不接管」。
  - 合规监测：档位改为 design／merge／release 三项、预设 none／design／merge；旧值按 spec-guard 兼容表读（旧 `plan` 读作 none，旧对象里合法的 spec／plan 忽略）；基线 schema 升到 v2，v1 照常可读。
  - 发布引擎：新预设 `design` 的发布档位按 human，不再提示读不懂。

- CI workflow 改名为 `CI`，在 macOS 系统 Python 3.9 与 3.13 上各跑一遍测试；新增汇总任务 `CI required`（规范 C1），顶层权限改为 `permissions: {}`、按任务放权（规范 C8）。

## 统一基线 713a3d8（2026-11-23）

各调用方当前钉的目标基线。此前的切片 1–17 见 README 的「现状」一节和 `docs/spec/`。
