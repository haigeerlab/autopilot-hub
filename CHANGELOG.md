# Changelog

本文件按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式记录 autopilot-hub 的变更。

hub 不发版本号：各仓库按 40 位提交 SHA 引用 hub，「需要升钉」的变更会在条目里注明（插件开发规范 H1）。待发布或尚未通知升钉的变更写在 `[Unreleased]`。

## [Unreleased]

### Added
- `docs/plugin-standard.md`：《插件开发规范》v1.1 正式存放处，附条目与 hub 检查的对应表。
- `LICENSE`（MIT）、`.gitignore`、`CLAUDE.md`、`AGENTS.md`、`README.en.md`、本文件。
- 合规监测（切片 18a–18d）：`audit/cli.py`、批准基线、合规周报 issue 与违规 issue、按审查轮次核验 Codex 批准。

### Changed
- CI workflow 改名为 `CI`，在 macOS 系统 Python 3.9 与 3.13 上各跑一遍测试；hub 主干要求这两个检查通过。

## 统一基线 713a3d8（2026-11-23）

各调用方当前钉的目标基线。此前的切片 1–17 见 README 的「现状」一节和 `docs/spec/`。
