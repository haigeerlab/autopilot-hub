"""Display a resolved pipeline; never execute its commands."""
import argparse
import json
from pathlib import Path
import sys


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "参数错误：请使用 show --repo <本地检出> [--json] [--templates <目录>]\n")

    def format_help(self):
        return (super().format_help().replace("usage:", "用法：")
                .replace("positional arguments:", "位置参数：")
                .replace("optional arguments:", "可选参数：")
                .replace("options:", "可选参数："))


def main(argv=None):
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline import model

    parser = _Parser(description="查看合并后的流程文件")
    parser._actions[0].help = "显示帮助并退出"
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="按段显示流程")
    show._actions[0].help = "显示帮助并退出"
    show.add_argument("--repo", required=True, help="本地仓库检出目录")
    show.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    show.add_argument("--templates", default=model.DEFAULT, help="模板目录")
    args = parser.parse_args(argv)
    try:
        if not Path(args.repo).is_dir():
            print(f"文件读取失败：仓库目录不存在或不是目录：{args.repo}", file=sys.stderr)
            return 2
        config = model.read_repo_pipeline(args.repo)
        if config is None:
            template = model.default_template_for(args.repo)
            if template is None:
                raise model.PipelineError(["没有 pipeline.yml，无法从风险配置识别流程类别，请声明 extends"])
            category = "frontend" if template == "site" else "plugin"
            notice = f"没有 pipeline.yml，按类别 {category} 套模板 {template}"
            print(notice, file=sys.stderr if args.json else sys.stdout)
            config = {"extends": template}
        result = model.resolve(config, args.templates)
    except model.PipelineError as error:
        for problem in error.problems:
            print(f"流程校验失败：{problem}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("文件读取失败：请检查仓库和模板目录、文件权限以及 UTF-8 编码", file=sys.stderr)
        return 2
    except ValueError:
        print("文件读取失败：风险配置格式错误，请检查 risk-tiers.yml 或 risk-tiers.json", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"模板：{result['template']}，声明回滚能力：{'是' if result['rollback'] else '否'}")
        for segment in model.SEGMENTS:
            steps = [step for step in result["steps"] if step["at"] == segment]
            if not steps:
                continue
            print(f"\n{segment}：")
            for step in steps:
                condition = f"  [{step['auto_when']}]" if step["auto_when"] else ""
                print(f"  {step['id']}  {step['by']}  {step['run']}{condition}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
