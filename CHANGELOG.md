# Changelog

本文件按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式记录 autopilot-hub 的变更。

hub 不发版本号：各仓库按 40 位提交 SHA 引用 hub，「需要升钉」的变更会在条目里注明（插件开发规范 H1）。待发布或尚未通知升钉的变更写在 `[Unreleased]`。

## [Unreleased]

### Added
- `docs/plugin-standard.md`：《插件开发规范》正式存放处（现为 v1.5），附条目与 hub 检查的对应表。
- `.github/dependabot.yml`：只升级 GitHub Actions，每月一次，冷却 7 天（规范 C9）。
- `LICENSE`（MIT）、`.gitignore`、`CLAUDE.md`、`AGENTS.md`、`README.en.md`、本文件。
- 合规监测（切片 18a–18d）：`audit/cli.py`、批准基线、合规周报 issue 与违规 issue、按审查轮次核验 Codex 批准。

- 规范卡 A1、A2 的共享检查：`rules/local_paths.py`、`rules/no_secrets.py`（共用 `rules/diffs.py`），接进可复用的 `rules.yml`，影子运行期只报告。**需要升钉**（改到 `rules/` 与 `.github/workflows/`）。

- 规范卡 B1、B5、F1 的共享检查：`rules/version_consistency.py`、`rules/release_tag.py`、`rules/commit_format.py`；B1、F1 接进 `rules.yml` 影子运行，B5 供发版命令和推送标签的 workflow 调用。**需要升钉**。

### Changed
- CI workflow 改名为 `CI`，在 macOS 系统 Python 3.9 与 3.13 上各跑一遍测试；新增汇总任务 `CI required`（规范 C1），顶层权限改为 `permissions: {}`、按任务放权（规范 C8）。

## 统一基线 713a3d8（2026-11-23）

各调用方当前钉的目标基线。此前的切片 1–17 见 README 的「现状」一节和 `docs/spec/`。
