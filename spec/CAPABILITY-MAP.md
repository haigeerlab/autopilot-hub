# Capability Map: autopilot hub

> 从自动驾驶第一版起，hub 自己的功能按 spec-guard 新流程开发。总需求、能力图和总体技术方案在 plugin-ecosystem 总账 `research/2026-11-26-自动驾驶目标/`（已批）；本图只列落在 hub 仓库的模块，编号对应总账能力图的行号。`docs/spec/` 里此前的切片规格留在原处，不迁。

## 目标

让 hub 在合并前验证通过后把 PR 交给 GitHub 自动合并、盯住 CI 和主干，出事时停下、收紧、通知用户，使用户只需确认需求，其余合并、发版由 autopilot 完成（自动驾驶第一版 R4、R6、S3）。

## 模块

| Module id | Responsibility | Depends on |
|---|---|---|
| hub-auto-merge | 总账能力图第 2 行：合并前验证通过后由 hub 写 `autopilot/verified`、打开 GitHub 自动合并（检查已全绿时直接合并）；本机 `watch` 盯 CI；T4 不打开；CI 变红按三种分；主干变红时停下、收紧、通知，不自动撤回；等太久就停下告诉用户；风险分级按 R4 改 | — |

Build order: hub-auto-merge

<!-- 总账能力图第 3 行 hub-release、第 9 行 hub-cleanup 开工时再加。 -->

---

## 审阅记录

- [x] 模块边界确认
- [x] 依赖方向单向无环
- [x] module id 已定稿
- [x] 构建顺序符合依赖拓扑

审阅人：用户（原话「批准」，同时批了产物语言设为中文）
日期：2026-11-27
