# Spec：切片 16，模板与体检（第三批）

> 第三批规格（用户已批准，原话'批准'）的切片 16，对应假设 C3、C9–C11。

本切片提供 plugin、npm、site 三个发布模板、仓库入口适配器、流程文件体检与草稿补丁，并把合并前追加步骤接入第二批编排。只依赖 Python 标准库，兼容 Python 3.9；YAML 复用 `checkup/yamlsub.py`。

## 仓库入口与执行环境

模板统一调用：

```sh
python3 -B "$AUTOPILOT_HUB/pipeline/entry.py" <入口名>
```

`pipeline/entry.py` 在当前工作目录（发布的一次性克隆根目录）读取 `.github/autopr/interface.yml` 的 `entries.<入口名>.run`。项目在这里登记自己已有的命令，例如：

```yaml
entries:
  release_prepare:
    run: 'scripts/release-prepare.sh "$AUTOPILOT_VERSION"'
```

入口名只允许 `[a-z0-9_]+`，非法名字或参数退出 `2`。登记值必须为非空字符串；文件不存在、无法读取、YAML 读不懂、结构不对或未登记入口时退出 `3`，输出：

```text
interface.yml 没登记 entries.<入口名>.run，这一步无法自动做，请在 pipeline.yml 里 replace 成自己的命令或 remove
```

合法命令通过 `['/bin/sh', '-c', run]` 执行，保持当前目录和环境，stdin 关闭，stdout / stderr 原样输出，命令退出码透传。发布引擎沿用切片 14 的版本、仓库和 issue 环境变量，并由 `Work.run_release` 设置 `AUTOPILOT_HUB` 为 hub 根目录绝对路径，覆盖继承环境中的同名值。

全部模板入口名清单：

| 模板 | 入口名 |
| --- | --- |
| plugin | `impact`、`check`、`release_prepare`、`release_publish`、`install_local`、`verify_install`、`release_record` |
| npm | `release_branch`、`release_check`、`pack_smoke`、`preflight`、`stage_publish`、`readback`、`fresh_install`、`release_wrap_up` |
| site | `build`、`ci_check`、`rollback_ready`、`deploy_preview`、`promote`、`verify_live` |

## 三个发布模板

仓库通过 `.github/autopr/pipeline.yml` 的 `extends: plugin`、`extends: npm` 或 `extends: site` 选模板，再按切片 13 的 `add`、`remove`、`replace` 表达差异。每步 id 唯一，合并前主链路仍由内核控制。

下表的自动档位表示 `auto_when: "release: <档位>"`；除标明人工的步骤外，均为 `by: ai`。`human` 是最低档，`release: human` 表示任何档位都自动执行。表中未注明的证据均为 `[stdout]`。人工步骤不设 `auto_when`，任何档位都等人亲手完成后确认，具体续跑语义沿用切片 14。

### plugin.yml

`class: plugin`，`rollback: false`。

| id | 段 | 入口或命令 | 自动档位 | 用途与证据 |
| --- | --- | --- | --- | --- |
| `impact` | post-merge | `impact` | human | 报影响面 |
| `full-test` | before-release | `check` | prepare | 全量检查；`stop_if: "exit != 0"` |
| `prepare` | before-release | `release_prepare` | prepare | 备好版本，例如项目已有的 release-prepare 脚本 |
| `approve` | before-release | `echo 档位允许自动发版` | auto-confirm | 档位不足时停下，等待你同意发版 |
| `tag-and-release` | release | `release_publish` | auto-confirm | 打 tag、发 release；`[stdout, url]` |
| `install-local` | release | `install_local` | auto-confirm | 本机 Claude、Codex 两侧安装 |
| `verify-install` | after-release | `verify_install` | auto-confirm | 逐文件核安装、doctor |
| `restart-codex` | after-release | `ask: 你（⌘Q 重开 ChatGPT／Codex，让新插件版本生效）` | 人工 | `by: human` |
| `reload-claude` | after-release | `在每个在用的 Claude Code 会话里打 /reload-plugins（不必 /clear）` | 人工 | `by: human` |
| `record` | after-release | `release_record` | auto-confirm | 记账、通知使用方 |

官方已支持在会话里不重启加载插件，`/reload-plugins` 仍须人在每个在用会话中亲手输入；⌘Q 重开 ChatGPT／Codex 也始终由人完成。普通插件在 `prepare` 档停于 `approve`，在 `auto-confirm` 档停于 `restart-codex`。四个流水线自用插件仍受切片 15 的内核上限约束，例如 delegate-plugin 设 `auto-confirm` 时实际为 `prepare`，`approve` 仍等人。

### npm.yml

`class: npm`，`rollback: false`。

| id | 段 | 入口或命令 | 自动档位 | 用途 |
| --- | --- | --- | --- | --- |
| `version-branch` | before-release | `release_branch` | prepare | 版本分支 |
| `candidate-check` | before-release | `release_check` | prepare | build、check:publish |
| `pack-smoke` | before-release | `pack_smoke` | prepare | 从 tarball 安装并冒烟 |
| `preflight` | before-release | `preflight` | prepare | 只读核对 npm whoami、版本未占用 |
| `stage-publish` | release | `stage_publish` | auto-confirm | npm stage publish，不需要 2FA |
| `approve-stage` | release | `npm stage approve（每个版本都要 2FA，只能你本人做）` | 人工 | `by: human`，由你批准暂存版本 |
| `readback` | after-release | `readback` | auto-confirm | npm view、dist-tags、tarball 200 |
| `fresh-install` | after-release | `fresh_install` | auto-confirm | 全新安装验证 |
| `move-latest` | after-release | `ask: 你（移动 latest）` | 人工 | `by: human` |
| `wrap-up` | after-release | `release_wrap_up` | auto-confirm | 收尾 PR |

`auto-confirm` 档运行到 `approve-stage` 停下。2FA 批准暂存版本每个版本都要由本人完成。

### site.yml

`class: site`，`rollback: true`。站点首次放凭据永远由人完成，首次运行模板前须先准备好。

| id | 段 | 入口 | 自动档位 | 用途与证据 |
| --- | --- | --- | --- | --- |
| `build` | before-release | `build` | prepare | 构建 |
| `ci-green` | before-release | `ci_check` | prepare | 核对 CI 已过 |
| `rollback-ready` | before-release | `rollback_ready` | prepare | 确认可回滚的上一次部署 ID，并打印留存；`[stdout]` |
| `deploy-preview` | release | `deploy_preview` | auto-confirm | 部署预览；`[stdout, url]` |
| `promote` | release | `promote` | auto | 切生产 |
| `verify-live` | after-live | `verify_live` | auto | 上线核验 |

满足内核上限条件且实际档位为 `auto` 时，site 模板全部执行。`rollback-ready` 的 id 符合切片 15 的 `rollback` 前缀要求；入口负责确认并输出上一次部署 ID，内核只检查声明和步骤存在。

`sandbox.yml` 的 `impact` 补上 `auto_when: "release: human"`，其余步骤保持不变。

## C14 流程文件与 F5 草稿

体检在 C13 后新增 C14「流程文件」，检查 `.github/autopr/pipeline.yml`：

- 文件存在且 `pipeline.model.resolve` 通过：`ok`。
- 文件存在但读不了或校验失败：`unknown`，detail 最多列出五条校验问题。
- 文件不存在：`missing`。

C14 只提醒，不改变 `conclusion` 的接管档位判断。只有 missing 才生成 F5；已有但非法的流程交人修正，不生成覆盖补丁。

F5 在体检输出目录生成 `pipeline.patch`，可用 `git apply` 新建仓库流程文件，草稿仅含：

```yaml
# 改这个文件算 T4，由人审后合并
extends: <类别>
```

类别先用 `pipeline.model.default_template_for(root)`：风险类别 plugin → plugin、frontend → site。没有猜到类别时，若根目录 `package.json` 可读且为对象，其中没有 `"private": true`，则使用 npm。仍无法判断时不生成补丁，F5 note 为「看不出类别（plugin / npm / site），请告诉我」。体检只写输出目录中的草稿，不应用、不提交。

## 合并前追加步骤

第二批 `orchestrator.flow.run` 在核心检查 check 通过后、审查或复用最终验收报告前，执行以下过程：

1. `Work.read_pipeline` 从一次性克隆中已抓取的基础分支提交读取 `.github/autopr/pipeline.yml`，不切换检出、不读取 PR head 的流程文件。基础分支没有该文件就跳过，不自动套默认模板。
2. 用 hub 模板 `resolve`，只取 `at == pre-merge` 的步骤，按合并后的顺序处理。配置无法解析或校验失败时停在 `pre-merge`。
3. `by: ai` 的命令复用 `Work.run_check`，在同一份 PR head 一次性克隆里执行，使用同样的环境、关闭的 stdin 和 15 分钟超时；追加步骤不受发布档位控制。
4. 非零退出或超时停在 `pre-merge`，不继续后面的追加步骤或审查。`by: human`、`by: external` 或 `ask:` 步骤立即停止，原因是「合并前追加步骤 <id> 要人做：…」。
5. 已执行步骤的结果追加到本机检查证据的 `checks`，保留 `cmd`、`exit`、`output_sha256` 和 `tail`，非零退出的结果也留存。审查通过后，机器人硬卡证据评论的 `checks` 同时包含核心检查和所有追加检查，命令为步骤原始 `run`，并展示各自输出尾部。

模板不能声明 pre-merge，仓库只能 add。核心检查、审查、准入和按已审 head SHA 合并的链路仍由内核执行；准入已有「所有 checks 退出 0」的硬卡，本切片沿用。

## 已知限制与验证

第一版引擎没有条件分支，因此 site 模板没有「失败就回滚」的自动步骤。`rollback-ready` 负责提前确认并留存可回滚部署 ID，发生问题时由人按项目现有做法回滚。

模板只登记通用入口名，具体命令由各项目的 interface.yml 提供，缺少入口会停下，不推测执行命令。本切片不实际发布插件、npm 包或站点。

`tests/test_templates.py` 覆盖模板加载与能力声明、人工步骤各档位等待、各模板执行停点、内核上限、入口退出码与原样输出；体检测试覆盖 C14 三态、五条问题上限和可应用的 F5；编排测试覆盖追加检查、失败、人工、超时、证据，以及基础分支读取和 PR head 克隆复用。

```sh
python3 -B -m unittest discover -s tests
/usr/bin/python3 -B -m unittest discover -s tests
```
