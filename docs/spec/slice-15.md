# Spec：切片 15，发布硬卡（第三批）

> 第三批规格（用户已批准，原话'批准'）的切片 15，对应假设 C6。

本切片在切片 14 的执行引擎内强制施加发布档位上限。档位按 `human < prepare < auto-confirm < auto` 排序，实际执行档位始终为原始档位与上限中较低的一个，不提高项目设定。只依赖 Python 标准库，兼容 Python 3.9。

## hub 上限配置与规则

配置固定从 hub 根目录的 `release-limits.json` 读取，与命令的当前目录及目标仓库检出目录无关：

```json
{
  "schema": "autopilot.release-limits/v1",
  "pipeline_own": [
    "haigeerlab/spec-guard-plugin",
    "haigeerlab/module-crew",
    "haigeerlab/delegate-plugin",
    "haigeerlab/agent-relay"
  ],
  "repo_caps": {"haigeerlab/kb-plugin": "auto-confirm"},
  "sandbox_repos": ["haigeerlab/autopilot-sandbox"]
}
```

内核按以下优先级确定上限，仓库流程文件不能覆盖：

1. 配置文件不存在、无法读取、编码或 JSON 损坏，或结构非法时，所有仓库的上限均为 `prepare`。合法配置必须含上述 schema、非空字符串列表 `pipeline_own` 和 `sandbox_repos`（允许空列表），以及映射 `repo_caps`；后者的键为非空仓库名，值只能为 `human`、`prepare`、`auto-confirm`、`auto`。
2. 仓库在 `pipeline_own` 中时，上限为 `prepare`，即使套用 `sandbox` 或 `site` 模板并具备回滚条件也不例外。仓库名比较忽略大小写。
3. 其他仓库从 `auto-confirm` 上限起步。仅当模板 `class == site`，或 `class == sandbox` 且仓库在 hub 配置的 `sandbox_repos` 中，同时合并结果 `rollback` 严格为布尔值 `true`、合并后的步骤至少有一个 `id` 以 `rollback` 开头时，才允许上限到 `auto`。非沙盒仓库选择 `sandbox` 模板仍最高 `auto-confirm`，降档说明包含「sandbox 模板只给沙盒仓库」。
4. 对不在 `pipeline_own` 的仓库，若 `repo_caps` 有明确上限，再与上述上限取更严者。因此 `haigeerlab/kb-plugin` 即使选择 `sandbox` 或 `site` 模板并具备回滚条件，也最高 `auto-confirm`；`repo_caps` 为 `auto` 也不能绕过自动发布条件。仓库名比较均忽略大小写；同名的大小写变体配置多个上限时，取更严者。

删除 `auto_classes` 配置键，代码固定认可 `site`，`sandbox` 另按 hub 的 `sandbox_repos` 判断。仓库自己的 `pipeline.yml` 只能选择模板，不能声明自己进入沙盒名单或覆盖仓库上限。

回滚条件检查最终合并流程，删除所有 `rollback` 开头的步骤就失去 `auto` 资格；`rollback-check` 同样符合前缀条件。此处仅判断声明和步骤是否存在，不证明实际回滚能力，也不额外触发自动回滚。

超过上限时降档执行，原因例如：

> delegate-plugin 是流水线自用插件，发布最高到 prepare（你设的是 auto-confirm）

仅实际档位低于原始档位时产生降档原因。上限配置读不懂时，原始档位为 `human` 或 `prepare` 也不附说明，实际档位不会提高。

## 模板与合并结果

模板新增必填字段 `class`，只允许 `plugin`、`npm`、`site`、`sandbox`；缺少或非法值汇总到 `PipelineError.problems`。`load_template` 和 `resolve` 均保留该字段，合并结果形如：

```json
{"template": "sandbox", "class": "sandbox", "rollback": true, "steps": []}
```

仓库差异文件仍只允许 `extends`、`add`、`remove`、`replace`，不能覆盖模板 `class` 或 `rollback`。类别取自模板声明，不根据模板文件名、仓库名称或风险类别猜测。

`pipeline/templates/sandbox.yml` 声明 `class: sandbox`，保留原 `rollback-check`，并在 `after-live` 末尾增加：

```yaml
  - id: rollback
    at: after-live
    run: "echo 回滚演练"
    by: ai
    auto_when: "release: auto"
```

## 引擎、续跑与预演

每次新发布或续跑，先按切片 14 规则读取默认分支当前提交的原始档位，再读取 hub 当前上限并计算实际档位。所有步骤的 `decision` 均使用实际档位，`pre-merge` 仍跳过。

跟踪 issue 的开始块新增 `level_raw` 与 `level_cap_reason`，原 `level` 记录实际档位：

```text
<!-- autopilot-release:v1 {"version":"1.2.3","commit":"...","template":"sandbox","level":"prepare","level_raw":"auto-confirm","level_cap_reason":"delegate-plugin 是流水线自用插件，发布最高到 prepare（你设的是 auto-confirm）","pipeline_sha256":"...","steps":["publish"]} -->
```

无降档原因时 `level_cap_reason` 为 `null`。开始时有原因就同时写入中文正文；返回结果也带 `level_raw`、`level`、`level_cap_reason`，原因加入 `notes`，供 CLI 打印。

续跑采用当前原始档位和当前上限，不沿用开始块记录的档位，也不改写历史开始块。没有确认而继续等待时、重试 stopped 步骤时、确认完成后判定后续步骤时，都保留本轮的档位说明。开始块记录的原提交、可信记录检查、流程指纹变化停机及人工确认语义沿用切片 14；人工确认仅授权对应 waiting 步骤，不提高后续步骤的档位。`class` 随完整合并结果参与流程指纹。

`--dry-run` 打印原始发布档位、实际发布档位、降档或从严原因；没有原因时打印「档位说明：未降档」。每步计划使用实际档位，即使之前有等待步骤也完整列出。预演不打开 issue、不创建标签、不克隆、不执行命令、不写评论或账本。

## 模块接口与验证

- `pipeline.limits.load_limits(path=DEFAULT)`：读取并校验 hub 配置；失败返回 `None`，交由上限计算从严处理。
- `pipeline.limits.effective_level(repo, raw_level, pipeline, limits)`：接收合法原始档位、合并流程和 `load_limits` 返回值，返回 `(实际档位, 中文原因或 None)`，不修改输入。

`tests/test_limits.py` 覆盖四个自用插件、各档位不提高、普通 plugin / npm 类别、site / 沙盒名单内 sandbox 的回滚门槛、kb-plugin 选择 sandbox 仍最高 auto-confirm、非沙盒仓库选择 sandbox 的限制说明、沙盒仓库可到 auto、仓库更严上限及大小写边界、删除回滚步骤、新配置键缺失或非法，以及配置读不懂且原始档位为 human / prepare 时没有说明。`tests/test_pipeline.py` 验证必填类别、非法类别和仓库覆盖拒绝；`tests/test_engine.py` 验证降档后的执行判定、开始块及中文正文、续跑重算、从严重试和 CLI 预演。全部 GitHub 与命令执行均使用假 I/O。

```sh
python3 -B -m unittest discover -s tests
/usr/bin/python3 -B -m unittest discover -s tests
```
