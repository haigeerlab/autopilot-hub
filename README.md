# autopilot-hub

haigeerlab 自动维护系统的中心仓库。各仓库共用的准入、合并、降级、度量规则和脚本都放在这里，各仓库按 40 位提交 SHA 引用。

> 系统名 autopilot（2026-11-19 定）。

## 现状

- **切片 1**（已退役，见切片 7）：原先准入脚本读取 repo-readiness 的接入结论、算信任上限；该部分已删除。规格留作历史：[docs/spec/slice-1.md](docs/spec/slice-1.md)。
- **切片 2**：出事时自动开 `autopilot:degrade` issue（撤回 autopilot 的 PR、它合并后主干变红、手动打 `autopilot:regression` 或 `autopilot:freeze`），只要有一个未关闭的降级 issue，准入就报“不会自动合并”；关闭 issue 即解除。规格见 [docs/spec/slice-2.md](docs/spec/slice-2.md)。
- **切片 3**（已退役，见切片 7）：原先在本机每周复检接入就绪度；`recheck/` 与示例已删除。规格留作历史：[docs/spec/slice-3.md](docs/spec/slice-3.md)。
- **切片 4**：风险分级。准入给每个 PR 定风险档 T0–T4（共用规则＋四类模板＋仓库补充，只能收紧不能放宽），写明理由和能否自动合（T0–T2 可，T3 要人合，T4 停下交人）；`python3 -B risk/cli.py paths --repo <检出> --json` 给 spec-guard 读高风险路径。规格见 [docs/spec/slice-4.md](docs/spec/slice-4.md)。
- **切片 5**：体检命令。`python3 -B checkup/cli.py --repo <本地检出> [--base main] [--run-check] [--out <目录>] [--json]` 在用户的 Mac 上对本地检出做 12 项体检（C1–C12：有没有能跑的检查命令、分支保护、主干 CI、密钥扫描、action 固定版本、锁文件、风险分级配置、指令文件的 autopilot 例外等），只提醒不卡接入，结尾说清现在能开到哪一档（只接管 Plan／接管到合并）；缺的项能补的只生成补丁和命令（分支规则集、`risk-tiers.json`、指令文件说明草稿），从不自动执行。搬自 repo-readiness `ac13f17` 的探针代码在 `checkup/`。规格见 [docs/spec/slice-5.md](docs/spec/slice-5.md)。
- **切片 6**：纠错沉淀。同一类错误不让 AI 犯第三次：`python3 -B correction/cli.py record --repo <owner/name> --class <类名> --summary "…" [--ref …]` 把纠正记成该仓库的 issue（标签 `correction`、`correction:<类名>`），同一类第 2 次开「晋升候选」issue 并问要不要沉淀；`classes --repo …` 列类名和次数；切片 2 的撤回降级同时记 `revert` 类。三个检查脚本 `rules/`（`no_weakened_assertion.py` 不许弱化断言、`suppression_budget.py` 压制只减不增且豁免要过期、`chinese_commit.py` 提交用中文）由可复用 workflow `.github/workflows/rules.yml` 提供，各出一条检查 `autopilot/rule/<名>`，只提醒不设为必需，调用示例 `examples/caller-autopilot-rules.yml`；晋升做法见 `skills/correction-promotion/SKILL.md`。规格见 [docs/spec/slice-6.md](docs/spec/slice-6.md)。
- **切片 7**：退役 repo-readiness 依赖。准入不再读接入结论、不再报信任上限，改报一句话结论“能不能自动合并”：风险档（T0–T2）、接管评论 `autopilot-takeover:v1` 里的合并为 `auto`、硬卡证据（第二批才有，现在恒为“还没有”）、没有未关闭的降级 issue，四项都满足才写“可以自动合并”，否则列出原因。仍然只报告，检查结果是 `neutral`；PR 上打 `autopilot:risk-T<n>` 标签；风险配置优先读 `.github/autopr/risk-tiers.yml`，没有再读 `.json`。规格见 [docs/spec/slice-7.md](docs/spec/slice-7.md)。
- 其余部分都还在设计阶段，设计稿在 plugin-ecosystem 总账的 `research/2026-11-18-自动PR系统设计/`。

## 规则

- **本仓库全部内容都属于 T4**：永不自动合并，每个改动都由人审阅后合并。原因是这里的规则和脚本就是裁判本身。
- 各仓库引用本仓库时，一律固定到 40 位 SHA，不用分支名或 tag。更新引用的 SHA，在各仓库里同样属于 T4。
- 判定脚本只用 Python 标准库、零依赖，在 macOS 的 `/usr/bin/python3`（3.9）和 ubuntu 上都要能跑。
