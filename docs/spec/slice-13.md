# Spec：切片 13，流程文件（第三批）

> 第三批规格（用户已批准，原话'批准'）的切片 13，对应假设 C2–C4。

本切片只定义格式、加载、合并、校验和打印，不执行命令，不等待确认，不访问外部服务。步骤执行属于切片 14，回滚能力声明供切片 15 使用。只依赖 Python 标准库，兼容 Python 3.9；YAML 使用现有 `checkup/yamlsub.py` 的严格子集解析器。

## 模板格式

模板位于 hub 的 `pipeline/templates/<name>.yml`，`name` 必须与文件名（不含 `.yml`）相同；`rollback` 必须是布尔值，`steps` 必须是列表，可以为空。本切片只提供 `sandbox.yml`，其中六步均为本机沙盒演练的假命令或人工确认声明。

```yaml
name: sandbox
rollback: true
steps:
  - id: publish
    at: release
    run: "echo 发布 ${AUTOPILOT_VERSION}"
    by: ai
    auto_when: "release: auto-confirm"
    stop_if: "exit != 0"
    evidence: [stdout]
```

段的固定顺序为 `pre-merge`、`post-merge`、`before-release`、`release`、`after-release`、`after-live`。模板不得声明 `pre-merge` 步骤：合并前的主链路由内核代码控制，仓库仅可追加步骤（C3）。

步骤字段：

| 字段 | 规则 |
| --- | --- |
| `id` | 必填，同一流程唯一，符合 `[a-z0-9]+(-[a-z0-9]+)*` |
| `at` | 必填，必须是上述六段之一；替换时可省略以沿用原段 |
| `run` | 必填，非空命令字符串；以 `ask:` 开头表示等待人工确认，此时 `by` 必须为 `human` |
| `by` | 必填，只允许 `ai`、`human`、`external` |
| `auto_when` | 可选，严格为 `release: <档位>`，档位只允许 `human`、`prepare`、`auto-confirm`、`auto` |
| `stop_if` | 可选，只允许 `exit != 0` 或 `missing: <证据名>`，证据名只允许 `stdout`、`url`、`file` |
| `evidence` | 可选，列表，元素只允许 `stdout`、`url`、`file`；缺省为 `[stdout]` |

可选条件缺省时，解析结果中的 `auto_when`、`stop_if` 为 `null`。显式填写非法值（包括 `null`）会报错。未知步骤字段和未知模板字段报错；`version` 与 `$` 开头的元数据键忽略。命令中的变量保持原样，不插值、不执行。

## 仓库差异文件与合并

仓库文件为 `.github/autopr/pipeline.yml`，只写相对模板的差异：

```yaml
extends: sandbox
remove: [verify]
replace:
  - id: publish
    run: echo 自定义发布
    by: ai
add:
  - id: tester-signoff
    at: before-release
    run: "ask: 测试人员"
    by: human
    after: confirm
```

顶层只允许 `extends`、`add`、`remove`、`replace`，忽略 `version` 和 `$` 开头的键，其他键报错。`extends` 必填，必须指向模板目录中的文件名，不允许包含路径。

1. 读取并校验模板。
2. `remove`：按 id 删除模板步骤。引用不存在的 id 报错。
3. `replace`：按 id 整步替换模板步骤；省略 `at` 时沿用原段，显式 `at` 可以移到其他段。其他字段完全以替换项为准，可选字段不沿用。引用不存在、重复替换、同一 id 同时删除和替换均报错。
4. `add`：按列表出现顺序逐项处理。默认追加到对应段末尾；可指定 `before: <id>` 或 `after: <id>`，不能同时指定。目标必须已存在且处于同一段，也可引用前面已添加的步骤。与当前流程重复的 id 报错；删除后可重新添加该 id。
5. 最终按固定段顺序排列，段内保持合并所得顺序；整步替换保留列表位置，再按段稳定分组。

模板不允许出现 `pre-merge`；仓库可以 `add` 到 `pre-merge`，不能 `remove` 或 `replace` 该段，也不能通过 `replace` 把其他步骤移入该段。所有可识别的校验问题集中在 `PipelineError.problems` 中文列表中，一次性报告；YAML 语法错误报告文件路径和行号。

## 接口与命令

- `load_template(name, templates_dir=DEFAULT)` 返回校验过的模板字典。
- `resolve(repo_config, templates_dir=DEFAULT)` 返回 `{"template": name, "rollback": bool, "steps": [...]}`，每步包含规范化的七个字段；不修改传入配置。
- `read_repo_pipeline(checkout)` 读取仓库流程文件，不存在时返回 `None`。
- `default_template_for(checkout)` 通过 `risk.tiers.read_repo_config_at` 读取风险类别：`plugin` → `plugin`，`frontend` → `site`，其他类别或没有配置 → `None`。

```sh
python3 -B pipeline/cli.py show --repo <本地检出>
python3 -B pipeline/cli.py show --repo <本地检出> --json
python3 -B pipeline/cli.py show --repo <本地检出> --templates <模板目录>
```

存在流程文件时，解析并打印合并结果。没有流程文件时按风险类别选择模板，并提示“没有 pipeline.yml，按类别 <类别> 套模板”；类别无法识别或所选模板不存在时报错。本切片没有提供 `plugin`、`site` 模板，可通过 `--templates` 指定目录。

文本输出按非空段分组，每步显示 `id  by  run  [auto_when]`，没有条件则不打印方括号；同时显示模板和回滚能力声明。`--json` 标准输出严格为 `resolve` 的结果，自动套模板的提示写到标准错误。

退出码：`0` 成功；`1` 流程或模板校验失败（逐条打印中文问题）；`2` 参数或文件读取错误（含编码错误、检出目录无效、风险配置读取失败）。

## 验证

`tests/test_pipeline.py` 和 `tests/fixtures/pipeline/` 覆盖模板校验、差异操作及顺序、重复及悬空引用、跨段插入、`pre-merge` 限制、字段取值、多问题聚合、类别推断和 CLI 文本／JSON／退出码。完整回归使用：

```sh
python3 -B -m unittest discover -s tests
/usr/bin/python3 -B -m unittest discover -s tests
```
