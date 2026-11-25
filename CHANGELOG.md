# Changelog

本文件按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式记录 autopilot-hub 的变更。

hub 不发版本号：各仓库按 40 位提交 SHA 引用 hub，「需要升钉」的变更会在条目里注明（插件开发规范 H1）。待发布或尚未通知升钉的变更写在 `[Unreleased]`。

## [Unreleased]

### Added
- `docs/plugin-standard.md`：《插件开发规范》正式存放处（现为 v1.7.1），附条目与 hub 检查的对应表。
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

### Changed
- CI workflow 改名为 `CI`，在 macOS 系统 Python 3.9 与 3.13 上各跑一遍测试；新增汇总任务 `CI required`（规范 C1），顶层权限改为 `permissions: {}`、按任务放权（规范 C8）。

## 统一基线 713a3d8（2026-11-23）

各调用方当前钉的目标基线。此前的切片 1–17 见 README 的「现状」一节和 `docs/spec/`。
