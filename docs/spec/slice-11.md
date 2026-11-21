# Spec：切片 11，收窄与计数（第二批）

> 2026-11-22，autopr-design。总账 `research/2026-11-18-自动PR系统设计/batch-2-auto-merge-spec.md`（第二批规格，用户已批准，原话"批准"）的切片 11，对应假设 B10–B12。

## 一、出事收窄（B10）

分两层：
1. **立刻生效**：降级 workflow 照旧在撤回、主干变红、回归、冻结时开降级 issue（切片 2）。只要有未关闭的降级 issue，准入就判"不会自动合并"，本机编排也会停下。这一层不依赖本机。
2. **长期收窄**：本机编排每次运行时，如果遇到未关闭的降级 issue，原因是撤回（`revert`）、主干变红（`main-red`）或回归（`regression`），而且还没有机器人发的 `autopilot-narrowed:v1` 评论，就按下面处理一次：
   - **spec-guard 项目**：
     - 先确定收窄哪个模块：从降级 issue 证据里的 PR，取它最新一条受信的接管评论中的模块。没有 PR，或这条评论里没写模块，就收窄整个项目。
     - 在一次性克隆里检出基础分支，运行 spec-guard 0.91 的 `project_config.py narrow --source autopilot --reason "<降级 #n：原因>" --confirm`，按 spec-guard 的规则降一格（合并 → Plan → 不接管）。
     - 以机器人身份提交，推到 `autopilot/narrow-<issue>` 分支，开 PR。改的是 `.agent/config.json`，属于 T4，由你审后合并。
     - 已经是最严一档时不开 PR，只在 issue 里说明。
   - **非 spec-guard 项目**：只在降级 issue 里留言，提醒你把 `takeover.yml` 的 `merge` 改为 `human`（T4）。
   - 处理完，在降级 issue 上留一条机器人评论，带 `autopilot-narrowed:v1` 标记，防止重复收窄。人发的同样标记不算。
   - `--dry-run` 不收窄。
   - 冻结（`freeze`）只停下，不收窄。

放宽仍然要你的原话，由你自己改配置。收窄不需要你点头，但收窄 PR 要你合并才生效；在那之前，未关闭的降级 issue 已经挡住了自动合并。

## 二、降级 issue 只有管理员关才算（B11）

降级 workflow 新增处理 `issues: closed` 事件：被关闭的 issue 带 `autopilot:degrade` 标签，而且关闭的人不是仓库 admin 的真人账号（机器人或没有 admin 权限的人），就自动重新打开，并留言说明。调用方的调用文件要在 `issues.types` 里加 `closed`（示例已更新），这是 T4，各仓库自己改。

## 三、三个计数（B12）

`python3 -B orchestrator/cli.py stats --repo <owner/name> [--json]`：
- **自动合并次数**：带 `autopilot:auto-merged` 标签的 PR 数。
- **其中被撤回或返修的次数**，两种情况各计一次，同一个 PR 只算一次：
  - 撤回：降级 workflow 在撤回时给原 PR 打 `autopilot:reverted` 标签；
  - 返修：合并后 14 天内，有 `autopilot:regression` issue 在正文里提到这个 PR（`#n` 或 PR 链接）。
- **因 Codex 不可用而停下的次数和总时长**：读本机 `~/.autopilot/ledger.jsonl` 里的 `codex_unavailable` 记录。

用只读的机器人令牌读 GitHub，用完作废。
