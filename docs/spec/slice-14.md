# Spec：切片 14，执行引擎（第三批）

> 第三批规格（用户已批准，原话'批准'）的切片 14，对应假设 C1、C5、C7、C8。

本切片将切片 13 的 `resolve` 结果串成可续跑的发布过程，只执行「合并后 → 发布前 → 发布 → 发布后 → 上线后」五段。`pre-merge` 始终跳过，但仍参与完整流程指纹计算。只依赖 Python 标准库，兼容 Python 3.9。不实现切片 15 的发布档位上限、降档或回滚执行，只读原始发布档位。

## 命令与退出码

```sh
python3 -B orchestrator/cli.py release --repo <owner/name> --version <版本>
python3 -B orchestrator/cli.py release --repo <owner/name> --version <版本> --dry-run
python3 -B orchestrator/cli.py release --repo <owner/name> --resume <跟踪 issue 号>
python3 -B orchestrator/cli.py release --repo <owner/name> --resume <跟踪 issue 号> --confirm "<用户原话>"
```

`--version` 和 `--resume` 必须且只能提供一个；`--resume` 必须是正整数，版本不能为空。`--confirm` 只用于续跑，不能为空；`--dry-run` 只用于新发布。参数组合错误在换取令牌前拒绝。

退出码：

- `0`：全部完成，或 dry-run 计划打印成功。
- `1`：按设计等人（waiting），或出问题停下（stopped）。输出中文原因、跟踪 issue 链接和续跑示例。
- `2`：参数、环境、令牌、身份、issue 或流程校验问题。stopped 带 `--confirm` 也属于此类错误。

发布不依赖 delegate。复用 `orchestrator/token.py` 的 `mint` / `revoke`，令牌限定当前仓库：`contents: read`、`issues: write`；dry-run 使用 `contents: read`、`issues: read`。换取成功后，无论完成、停下或出错，均在 `finally` 中作废。

## 默认分支、流程与原始档位

开始时读取仓库默认分支和该分支当前提交，以此提交读取流程、风险配置和接管档位，避免读到不同时间点的配置。记录的 `commit` 用于所有后续命令执行。流程文件为基础分支 `.github/autopr/pipeline.yml`，合并与校验复用切片 13 的 `resolve`；hub 模板来自 `pipeline/templates/`。

没有流程文件时，沿用切片 13 的类别规则：基础分支 `risk-tiers.yml` 优先于 `risk-tiers.json`，`plugin` → `plugin` 模板、`frontend` → `site` 模板。类别无法识别或模板不存在则退出 `2`，不会打开发布 issue。当前 hub 仅提供 `sandbox` 模板，其他模板缺失时必须报错。

`read_release_level(io, repo, base_ref, notes)` 返回 `human`、`prepare`、`auto-confirm`、`auto` 之一，顺序由低到高：

- 基础分支存在 `spec/CAPABILITY-MAP.md`：复用 `base_file`、`read_sg_config` 读取 `.agent/config.json`，只看项目级 `takeover`，不看 `moduleTakeover`。预设 `none`、`plan`、`merge` 的发布均为 `human`。对象只取 `release` 项；缺省或非法按 `human`。
- 其他项目：读取基础分支 `.github/autopr/takeover.yml` 的 `release` 项。没有文件按 `human`，缺省、非法或读不懂也按 `human`。
- 读不懂时向 `notes` 加中文说明；发布输出会显示说明。

## 每一步的判定

按段和段内既定顺序执行，在第一条 waiting 或 stopped 处结束本轮。

| 步骤 | 结果与说明 |
| --- | --- |
| `run` 以 `ask:` 开头 | waiting，「等 <谁> 确认」 |
| `by: human` | waiting，「这一步要你本人做：<run>」 |
| `by: external` | waiting，「这一步由外部系统做：<run>，做完告诉我」 |
| `by: ai`，没有 `auto_when` | waiting，说明「不写就永远等人」 |
| `by: ai`，`auto_when: "release: <L>"`，当前档位 ≥ L | 执行 |
| `by: ai`，当前档位低于 L | waiting，「当前发布档位 <当前> 低于这一步需要的 <L>，要你确认后才做」 |

waiting 是按设计等人，stopped 表示要先修好再重试。确认 waiting 时，`by: ai` 且 `run` 不以 `ask:` 开头的步骤表示获准执行，现在运行命令；`ask:`、human、external 步骤表示人已完成，直接记录 done，不执行命令。

## 命令与证据

需要执行时才创建一次性克隆，复用 `Work` 的认证与 fetch 方式，抓取默认分支和开始时记录的提交，随后 detach 到该提交。同一次调用的后续步骤共用该克隆；续跑另建克隆，仍从开始提交执行。

发布克隆另设不含凭据的 `origin`（`https://github.com/<repo>`），配置基础分支的 fetch refspec 并抓取到 `refs/remotes/origin/<base_ref>`，保留 `refs/remotes/base`，同时抓取 `+refs/tags/*:refs/tags/*`；检出仍固定在开始提交、不建本地分支，步骤不继承 hub 的 Git 认证，发版准备只做本地提交。

命令固定为 `['/bin/sh', '-c', run]`，cwd 是克隆根目录，stdin 为 `DEVNULL`，stdout 和 stderr 合并捕获。环境为当前环境删除 `GH_TOKEN`、`GITHUB_TOKEN`、`GH_ENTERPRISE_TOKEN`，再设置：

- `AUTOPILOT_VERSION`：开始块中的版本。
- `AUTOPILOT_REPO`：`owner/name`。
- `AUTOPILOT_RELEASE_ISSUE`：跟踪 issue 号的字符串。

命令不继承克隆专用的 git 认证环境。30 分钟超时记为退出码 `124`，保留已有输出并加中文超时说明；退出码非 `0` 一律 stopped，不依赖 `stop_if`。

证据格式：

- `stdout`：`{"sha256": "<合并输出的 SHA-256>", "tail": ["最后最多 40 行"]}`，即使输出为空也可取证。
- `url`：合并输出中的第一个 `http://` 或 `https://` 链接。
- `file`：第一条 `FILE: <路径>` 指向的现存普通文件，记录 `{"path": "<克隆内相对路径>", "sha256": "<文件 SHA-256>"}`。允许克隆内的绝对路径，拒绝越界路径、指向克隆外的符号链接、目录和不存在的文件；首条路径不合格时不改用第二条。

每步 `evidence` 声明的每种证据都必须存在，缺少任何一种就 stopped，并列出缺什么。`stop_if: "missing: <x>"` 额外要求证据 x，即使 `evidence` 列表未包含它。stdout 总会记录，url / file 找到时也会记录。

## 跟踪 issue 与机器块

非 dry-run 开始时，以 App 机器人身份打开 issue，标题为 `autopilot 发布 <repo> v<version>`，标签为 `autopilot:release`。标签不存在时先通过 API 创建。正文中文介绍全过程，并附：

```text
<!-- autopilot-release:v1 {"version":"1.2.3","commit":"...","template":"sandbox","level":"auto","pipeline_sha256":"...","steps":["publish"]} -->
```

`pipeline_sha256` 为完整 resolve 结果 `json.dumps(result, sort_keys=True)` 的 UTF-8 SHA-256，包含 `pre-merge` 段与回滚声明。`steps` 只列本执行引擎实际负责的五段步骤 id。

每步写一条机器人评论：中文状态与原因，附机器块：

```text
<!-- autopilot-release-step:v1 {"id":"publish","status":"done","exit":0,"evidence":{},"reason":null,"confirmed_words":null} -->
```

`status` 只用 `done`、`stopped`、`waiting`；没执行命令时 `exit` 为 `null`。执行完成的 done 评论用折叠区附输出最后 40 行。人工确认的 done 记录将 `confirmed_words` 原样保留，不去空格、不改换行、不重写措辞。

全部完成后，再发一条中文总结，每步状态一行；关闭 issue；往本机 `~/.autopilot/ledger.jsonl` 追加：

```json
{"event":"release_done","repo":"owner/name","version":"1.2.3","issue":9,"at":"2026-11-22T01:00:00Z"}
```

## 续跑与可信记录

issue 必须为 open 的普通 issue，并有合法、可信的开始块，否则退出 `2`。开始块使用专门的 issue 判定，只认 `bot-identity.json` 中的数字 ID、Bot 类型，且正文未编辑的记录；GitHub 不在 App 创建的 issue 上返回 `performed_via_github_app`，因此允许其为 null，非 null 时仍必须匹配 App slug。步骤块继续复用 `rest_comment` 和 `admission.decide.trusted`，要求 App slug、数字 ID、Bot 类型匹配且未编辑。人发的、其他 App 发的、被编辑过的块不可信。

REST issue 的 `updated_at` 会随评论或标签变化，不能直接当正文编辑时间。`LocalIO.release_issue` 在时间变化时补读 GraphQL `lastEditedAt`：正文从未编辑则以创建时间送入 issue 判定，正文编辑过则拒绝。读取不到编辑时间就报错，不放宽身份检查。评论仍直接使用 REST 的创建、更新时间校验。

机器块必须位于正文或评论末尾，使用 JSON 解码器识别完整对象，允许用户原话包含 `-->` 等字符。输出折叠区中的伪造块不能覆盖真实步骤记录。续跑按流程顺序还原日志，已 done 的步骤跳过。

每次续跑重新读取默认分支当前提交上的流程并 resolve。指纹与开始块不同则评论并打印「发布进行中流程文件变了，请重新发起」，退出 `1`，不执行命令、不确认步骤。档位也重新读取，以当前值判定未完成步骤。

- 最后一条有效步骤记录是 waiting：不带 `--confirm` 时只输出「还在等：<原因>」及链接、续跑提示，退出 `1`，不评论、不执行、不写账本；带 `--confirm` 时按下面的步骤类型处理，保存原话，成功后继续。
- 确认 `by: ai` 且 `run` 不以 `ask:` 开头的 waiting 步骤时，在开始块记录的提交克隆里执行命令，并原样保存 `confirmed_words`；成功记 done 后继续，失败记 stopped 并退出 `1`，与后续步骤共用同一份克隆。`ask:`、human、external 步骤的确认仍只记 done，不执行命令。
- 最后一条有效步骤记录是 stopped：不带 `--confirm` 时重试该步；带 `--confirm` 报错退出 `2`，不能靠确认跳过问题。
- 所有步骤已经 done、但尚未关闭时：补发总结、关闭并记账。

## dry-run 与测试

`--dry-run` 不开 issue、不创建标签、不克隆、不运行命令、不写评论、不写账本。打印当前发布档位，以及每一步「会执行」或「会停下等人（原因）」，退出 `0`。即使前面会等人，也展示后续每步的判定。

`pipeline/engine.py` 的外部 I/O 全部通过传入的 `io`；`tests/test_engine.py` 使用假 GitHub、假克隆与假命令，覆盖档位、判定矩阵、五段执行、所有证据、停机、开始块与步骤块、总结关闭记账、续跑可信性和 dry-run。CLI 与 LocalIO 的边界测试还验证令牌权限与作废、环境、超时、文件越界拒绝、固定提交和 issue 编辑时间。

```sh
python3 -B -m unittest discover -s tests
/usr/bin/python3 -B -m unittest discover -s tests
```
