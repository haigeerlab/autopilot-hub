# Spec：切片 6，纠错沉淀（第一批）

> 2026-11-21，autopr-design。用户要求写纠错沉淀规格（原话"写纠错沉淀规格"）。用户已批准本规格与 A1–A8（原话"批准"）。
> 上游（总账）：方向文档 `research/2026-11-21-自动接管方向.md` 第二节第 6 条（纠错沉淀，最优先）；设计 r6 §4（纠错闭环）、总账 `research/2026-11-18-自动PR系统设计/lint-starter.md`（五个要求，A13、A14、A15）；总账 `research/2026-11-18-自动PR系统设计/skills/correction-promotion/SKILL.md` 草案（已定稿为 hub 的 `skills/correction-promotion/SKILL.md`）。依据是 Lauren Tan 的倒金字塔和 pstack `/correct`：要改的是仓库，不是提示词。

## 0. 假设（请先确认）

| # | 假设 | 不成立时 |
| --- | --- | --- |
| A1 | **纠正事件记成被纠正仓库里的 GitHub issue**：标签 `correction` 加 `correction:<类名>`，正文带机器可读块 `<!-- autopilot-correction:v1 {...} -->`。用 agent 能用的 `gh` 开（机器人身份建好前用你本机的 `gh`） | 记在本机文件或总账（别的会话、CI 都读不到） |
| A2 | **谁来记**：第一版两个来源——① 你在会话里纠正 AI 时，AI 用 `record` 命令记一条（你说"记一下"或 AI 判断这是一次纠正，记之前给你看类名）；② 切片 2 的撤回降级同时记一条 `correction:revert`。Codex 审查里重复出现的同类意见放第二批 | 只记你明说的 |
| A3 | **类名由 AI 先对已有类名**：匹配不上就起一个新类名（小写英文加连字符，如 `weakened-assertion`），新类名在记录时一起给你看 | 新类名都要你先批 |
| A4 | **同一类第 2 次就提醒**：`record` 记完发现这一类已有 ≥2 条（不论开关），就在该仓库开（或更新）一个"晋升候选"issue，标签 `correction:candidate`，列出这一类的全部事件，并在会话里提醒你"要不要沉淀成规则" | 第 3 次再提醒 |
| A5 | **晋升走 skill，不自动做**：你说"沉淀"后，AI 按 `correction-promotion` skill 起草（优先架构、类型、检查器，最后才是文字规则）→ 开一个独立 PR → Codex 审 → 你点头合并。skill 放在 hub 的 `skills/correction-promotion/`，等 autopilot 打包成插件时一起装 | 自动开晋升 PR |
| A6 | **第一批四条规则里，三条由 autopilot 落地、一条归 spec-guard**：①"不许删减测试断言"、②"压制只减不增、豁免要过期"、④"提交用中文"由 hub 提供检查；③"改动不超出声明范围"已经是 spec-guard 的越界检查（S32、合并方案第 2 节），autopilot 不另做一套 | autopilot 也做 ③ |
| A7 | **规则检查以可复用 workflow 提供**，仓库自愿接入（一个十几行的调用文件，T4）；**先不设为必需检查**：红了只提醒，跑满两周、误报为 0 再由你决定要不要设为必需 | 一上来就设为必需 |
| A8 | 每条规则都满足 `lint-starter.md` 的五个要求：报错按六行模板（含"改成""不要"）、一正一反测试、在真实历史出错提交上证明会失败、只拦新增、豁免只能写在出错那一行且带过期日 | — |

## 1. 目标

同一类错误不让 AI 犯第三次：第一次修，第二次提醒你沉淀，沉淀成检查后再犯就在 CI 上直接被拦。

## 2. 命令

```bash
# 记一条纠正事件（在被纠正仓库的本地检出里，或用 --repo 指明）
python3 -B <hub>/correction/cli.py record --repo <owner/name> --class <类名> --summary "<一句话：错在哪、该怎样>" [--ref <PR、提交或会话链接>]

# 列出某仓库的类名和每类次数（给 AI 对已有类名用）
python3 -B <hub>/correction/cli.py classes --repo <owner/name> [--json]
```

- `record` 开 issue 后，如果这一类达到 2 条，开或更新"晋升候选"issue，并在输出里写"要不要沉淀"。
- 同一个 `--ref` 不重复记。
- 只写 issue，不改仓库文件。

## 3. 第一批规则

| 规则 | 检查什么 | 只拦新增 | 报错里的"改成" |
| --- | --- | --- | --- |
| `no-weakened-assertion` | PR 的测试文件里，删除行中的断言标记多于新增行（复用切片 4 的判定） | 是（只看本 PR 补丁） | 保留原断言；行为确实要变，在 PR 写明原因并转交人审 |
| `suppression-budget` | ① PR 里压制注释净增加；② 每个 `autopilot-allow` 豁免格式完整、没过期 | ① 是；② 全仓扫（过期就该失败） | `# autopilot-allow <规则>: <理由> until <YYYY-MM-DD> approved-by <人>` |
| `chinese-commit` | PR 标题、正文和每个提交说明里有中文（`fix:`、`feat:` 这类前缀、路径、命令、代码标识符除外）；仓库显式声明对外开源英文的跳过 | 是（只看本 PR） | 用中文写清改了什么、为什么 |

每条规则一个检查脚本，放在 hub 的 `rules/`；可复用 workflow `rules.yml` 依次跑三个，各出一条检查结果。

## 4. 放在哪里

| 东西 | 位置 |
| --- | --- |
| `correction/`（`record`、`classes`） | autopilot-hub |
| `rules/` 三个检查、`.github/workflows/rules.yml` | autopilot-hub（T4） |
| `skills/correction-promotion/SKILL.md`（从设计目录的草案定稿：`autopr` 改 `autopilot`，补"六行报错模板"和"失败证明"格式） | autopilot-hub |
| 切片 2 撤回降级时同时记 `correction:revert` | `degrade/run.py` 小改 |
| 各仓库接入 `rules.yml` 的调用文件 | 各仓库，经 ecosystem-lead 请负责人提交 |

## 5. 验收标准

1. 单元测试：`record` 的开 issue、类名匹配、同一 `--ref` 去重、达到 2 条开候选；三条规则各自一正一反；豁免格式错误和过期各一例。GitHub 用假传输，不联网。
2. **失败证明**：三条规则各在一个真实的历史提交上跑出失败（例如某个删了断言的 PR、某个新增 `# noqa` 的 PR、某个英文提交说明），在当前 main 上跑出通过。贴进 PR 描述。
3. 在 hub 自己身上接入 `rules.yml` 跑一次。
4. 演练一次完整流程：在 hub 上记两条同类事件 → 出现候选 issue → 用 skill 起草一个晋升 PR（不合并，演练后关闭）。

## 6. 依赖与分工

| 谁 | 做什么 |
| --- | --- |
| autopr-design | 规格、实现、测试、失败证明、hub 的 PR、演练 |
| sg-coord | 规则 ③"不超出声明范围"已在它的越界检查里，不需新做 |
| 你 | 批准本规格（含 A1–A8）；合并 hub 的 PR；决定哪些仓库接入 `rules.yml`；两周后决定是否设为必需检查 |
