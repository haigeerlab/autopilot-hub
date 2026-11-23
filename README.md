# autopilot-hub

haigeerlab 自动维护系统的中心仓库。各仓库共用的准入、合并、降级、度量规则和脚本都放在这里，各仓库按 40 位提交 SHA 引用。

> 系统名 autopilot（2026-11-19 定）。

## 现状

- **切片 1**（已退役，见切片 7）：原先准入脚本读取 repo-readiness 的接入结论、算信任上限；该部分已删除。规格留作历史：[docs/spec/slice-1.md](docs/spec/slice-1.md)。
- **切片 2**：出事时自动开 `autopilot:degrade` issue（撤回 autopilot 的 PR、它合并后主干变红、手动打 `autopilot:regression` 或 `autopilot:freeze`），只要有一个未关闭的降级 issue，准入就报“不会自动合并”；关闭 issue 即解除。规格见 [docs/spec/slice-2.md](docs/spec/slice-2.md)。
- **切片 3**（已退役，见切片 7）：原先在本机每周复检接入就绪度；`recheck/` 与示例已删除。规格留作历史：[docs/spec/slice-3.md](docs/spec/slice-3.md)。
- **切片 4**：风险分级。准入给每个 PR 定风险档 T0–T4（共用规则＋四类模板＋仓库补充，只能收紧不能放宽），写明理由和能否自动合（T0–T2 可，T3 要人合，T4 停下交人）；`python3 -B risk/cli.py paths --repo <检出> --json` 给 spec-guard 读高风险路径。规格见 [docs/spec/slice-4.md](docs/spec/slice-4.md)。
- **切片 5**：体检命令。`python3 -B checkup/cli.py --repo <本地检出> [--base main] [--run-check] [--out <目录>] [--json]` 在用户的 Mac 上对本地检出做 14 项体检（C1–C14：有没有能跑的检查命令、分支保护、主干 CI、密钥扫描、action 固定版本、锁文件、风险分级配置、指令文件的 autopilot 例外、给 spec-guard 的风险路径清单是否最新、流程文件是否合法等），只提醒不卡接入，结尾说清现在能开到哪一档（只接管 Plan／接管到合并）；缺的项能补的只生成补丁和命令（分支规则集、`risk-tiers.yml`、指令文件说明草稿、`risk-paths.json`、`pipeline.patch`），从不自动执行。搬自 repo-readiness `ac13f17` 的探针代码在 `checkup/`。规格见 [docs/spec/slice-5.md](docs/spec/slice-5.md)。
- **切片 6**：纠错沉淀。同一类错误不让 AI 犯第三次：`python3 -B correction/cli.py record --repo <owner/name> --class <类名> --summary "…" [--ref …]` 把纠正记成该仓库的 issue（标签 `correction`、`correction:<类名>`），同一类第 2 次开「晋升候选」issue 并问要不要沉淀；`classes --repo …` 列类名和次数；切片 2 的撤回降级同时记 `revert` 类。三个检查脚本 `rules/`（`no_weakened_assertion.py` 不许弱化断言、`suppression_budget.py` 压制只减不增且豁免要过期、`chinese_commit.py` 提交用中文）由可复用 workflow `.github/workflows/rules.yml` 提供，各出一条检查 `autopilot/rule/<名>`，只提醒不设为必需，调用示例 `examples/caller-autopilot-rules.yml`；晋升做法见 `skills/correction-promotion/SKILL.md`。规格见 [docs/spec/slice-6.md](docs/spec/slice-6.md)。
- **切片 7**：退役 repo-readiness 依赖。准入不再读接入结论、不再报信任上限，改报一句话结论“能不能自动合并”：风险档（T0–T2）、接管评论 `autopilot-takeover:v1` 里的合并为 `auto`、硬卡证据（第二批才有，现在恒为“还没有”）、没有未关闭的降级 issue，四项都满足才写“可以自动合并”，否则列出原因。仍然只报告，检查结果是 `neutral`；PR 上打 `autopilot:risk-T<n>` 标签；风险配置优先读 `.github/autopr/risk-tiers.yml`，没有再读 `.json`。规格见 [docs/spec/slice-7.md](docs/spec/slice-7.md)。
- **切片 8**：准入只认 autopilot 机器人（`bot-identity.json` 里的 App slug 与数字 ID）发出、且没被编辑过的接管评论；不合格的忽略并在摘要里计数；hub 没配机器人身份时永远“不会自动合并”。规格见 [docs/spec/slice-8.md](docs/spec/slice-8.md)。
- **切片 9**：硬卡证据。准入读机器人发的 `autopilot-evidence:v1` 评论，只认绑定当前 head SHA 的那条：检查全过、审查完成且没有 blocking；接管评论带 spec-guard 模块时还要最终验收标记。规格见 [docs/spec/slice-9.md](docs/spec/slice-9.md)。
- **切片 10**：本机编排 `orchestrator/cli.py run --repo <owner/name> --pr <n> [--dry-run]`：以机器人身份发接管评论、在一次性克隆里跑检查、调 delegate 审查（第 2 轮对照第 1 轮复审）、发证据、用同一份准入代码重算，然后按审过的 head SHA 合并并读回。规格见 [docs/spec/slice-10.md](docs/spec/slice-10.md)。
- **切片 11**：收窄与计数。本机编排遇到撤回、主干变红、回归引起的降级 issue 时，在 spec-guard 项目里用 spec-guard 的 `narrow` 开一个收窄 PR（T4，你合并），其他仓库留言提醒；降级 issue 只有仓库管理员本人关闭才算解除，否则自动重开；`orchestrator/cli.py stats` 给出自动合并、被撤回或返修、因 Codex 不可用停下三个数。规格见 [docs/spec/slice-11.md](docs/spec/slice-11.md)。
- **切片 12**：模块产物改档。spec-guard 模块 PR 里改的本模块 `spec/<模块>.md`、`tasks/<模块>/**`，由本机编排在 head 上用 spec-guard 的 `map_view.py` 核对批准仍有效、写进证据后，从 T4 降为 T2；能力图、别的模块的产物和 `.agent/**` 永远 T4。规格见 [docs/spec/slice-12.md](docs/spec/slice-12.md)。
- **切片 13**：流程文件。仓库 `.github/autopr/pipeline.yml` 只写相对 hub 模板的差异，支持追加、定位插入、删除和整步替换；`python3 -B pipeline/cli.py show --repo <本地检出> [--json]` 校验并按段打印合并结果，只展示不执行。规格见 [docs/spec/slice-13.md](docs/spec/slice-13.md)。
- **切片 14**：执行引擎。`orchestrator/cli.py release --repo <owner/name> --version <版本> [--dry-run]` 按流程执行发布五段，以机器人跟踪 issue 逐步留证据，等人确认或失败时停下；`--resume <issue 号> [--confirm "用户原话"]` 从原提交续跑，只认未编辑的机器人记录。规格见 [docs/spec/slice-14.md](docs/spec/slice-14.md)。
- **切片 15**：发布硬卡。内核按 hub 的 `release-limits.json` 限制实际发布档位：四个流水线自用插件最高 `prepare`，`kb-plugin` 最高 `auto-confirm`；仅 `site` 或沙盒名单内的 `sandbox` 流程在声明回滚并含回滚步骤时可到 `auto`，并叠加仓库明确上限取更严者，其余最高 `auto-confirm`；上限配置读不懂时从严到 `prepare`。新发布、续跑与预演都重算，仅实际降档时说明原因。规格见 [docs/spec/slice-15.md](docs/spec/slice-15.md)。
- **切片 16**：模板与体检。提供 plugin、npm、site 发布模板，通过 `pipeline/entry.py` 调用项目 `interface.yml` 登记的命令；保留插件重开与会话加载、npm 2FA 等人工步骤。体检新增 C14 流程校验，缺失时生成 T4 草稿 `pipeline.patch`；合并前在核心检查通过后执行基础分支流程追加的 pre-merge 步骤，结果进入本机和评论证据。首版无条件分支，不含自动回滚步骤。规格见 [docs/spec/slice-16.md](docs/spec/slice-16.md)。
- **切片 18**：合规监测。`audit/baselines.json` 登记每个仓库批准的接管档位、钉和体检状态（改它算 T4）；18a 已能检查接管档位有没有被改宽、钉有没有变、体检有没有退化、有没有挂着的降级。18b 读 spec-guard 的 map_view，检查已开工的模块 Plan 是否真被批过、Codex 批的是否在档位允许之内、重要需求的 Spec 是否由人批。18c 加了命令 `python3 -B audit/cli.py`，结果写进 hub 的"autopilot 合规周报" issue；有违规时，每个仓库每类违规开一个 `autopilot:audit` issue，之后只追加评论，从不自动关闭。基线已登记 agent-relay 和沙盒。规格见 [docs/spec/slice-18.md](docs/spec/slice-18.md)。
- **module-crew 合并第一段**：`orchestrator/cli.py batch --repo <owner/name> [--dry-run]` 每次推进一个本地合并模块，由 autopilot 推 batch 分支、开 PR、补验收并按现有准入合并；规格见 [docs/spec/module-crew-batch.md](docs/spec/module-crew-batch.md)。
- 其余部分都还在设计阶段，设计稿在 plugin-ecosystem 总账的 `research/2026-11-18-自动PR系统设计/`。

## 规则

- **本仓库全部内容都属于 T4**：永不自动合并，每个改动都由人审阅后合并。原因是这里的规则和脚本就是裁判本身。
- 各仓库引用本仓库时，一律固定到 40 位 SHA，不用分支名或 tag。更新引用的 SHA，在各仓库里同样属于 T4。
- 判定脚本只用 Python 标准库、零依赖，在 macOS 的 `/usr/bin/python3`（3.9）和 ubuntu 上都要能跑。
