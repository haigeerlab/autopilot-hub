# autopilot-hub

haigeerlab 自动维护系统的中心仓库。各仓库共用的准入、合并、降级、度量规则和脚本都放在这里，各仓库按 40 位提交 SHA 引用。

> 系统名 autopilot（2026-11-19 定）。

## 现状

- **切片 1**（只报告）：准入脚本读取 repo-readiness 出具的接入结论，算出 PR 的信任上限。试点仓库是 agent-relay。规格见 [docs/spec/slice-1.md](docs/spec/slice-1.md)。
- **切片 2**：出事时自动开 `autopilot:degrade` issue（撤回 autopilot 的 PR、它合并后主干变红、手动打 `autopilot:regression` 或 `autopilot:freeze`），准入读它压低上限；关闭 issue 即解除。规格见 [docs/spec/slice-2.md](docs/spec/slice-2.md)。
- 其余部分都还在设计阶段，设计稿在 plugin-ecosystem 总账的 `research/2026-11-18-自动PR系统设计/`。

## 规则

- **本仓库全部内容都属于 T4**：永不自动合并，每个改动都由人审阅后合并。原因是这里的规则和脚本就是裁判本身。
- 各仓库引用本仓库时，一律固定到 40 位 SHA，不用分支名或 tag。更新引用的 SHA，在各仓库里同样属于 T4。
- 判定脚本只用 Python 标准库、零依赖，在 macOS 的 `/usr/bin/python3`（3.9）和 ubuntu 上都要能跑。
