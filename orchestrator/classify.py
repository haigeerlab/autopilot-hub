"""Classify GitHub Actions failures from run and job payloads."""

import re


_TEST_STEP = re.compile(
    r"(?<![a-z0-9])(?:tests?|pytest|unittest|validate|checks?(?!\s+out\b)|"
    r"shellcheck|typecheck)(?![a-z0-9])|测试|校验",
    re.IGNORECASE,
)
_CONFIG_ERROR = re.compile(
    r"(?:error:\s*)?(?:invalid workflow file\b|"
    r"(?:failed|unable) to parse (?:the )?workflow\b|"
    r"workflow pars(?:e|ing) (?:error|failed)\b|"
    r"(?:failed|unable) to (?:resolve|find) (?:the )?action\b|"
    r"(?:cannot|can't|could not) find (?:the )?(?:action\b|['\"]action\.ya?ml['\"])|"
    r"(?:the )?action(?: ['\"]?[\w./@-]+['\"]?)? (?:was )?not found\b)",
    re.IGNORECASE,
)
_NON_FAILURE = ("success", "skipped", "neutral", "cancelled")


def classify_failure(run, jobs):
    """Return (category, Chinese reason) for a failed run and its list of jobs.

    A startup failure is conclusive. Otherwise any failed or unclear job
    classified as test wins, followed by config, then env.
    """
    conclusion = run.get("conclusion") if isinstance(run, dict) else None
    if conclusion == "startup_failure":
        return "config", "工作流启动失败（startup_failure）。"
    if conclusion != "failure":
        return "test", "运行结论缺失或无法确认失败类型。"

    result = None
    if _is_config_error(run.get("name")):
        result = "config", "运行名称显示工作流解析或 Action 查找失败。"

    if not isinstance(jobs, list):
        return result or ("test", "缺少运行的 job 列表。")

    for job in jobs:
        conclusion = job.get("conclusion") if isinstance(job, dict) else None
        if conclusion in _NON_FAILURE:
            continue
        if conclusion != "failure":
            return "test", "存在结论缺失或无法判断的 job。"

        failure = _classify_failed_job(job)
        if failure[0] == "test":
            return failure
        if result is None or (failure[0] == "config" and result[0] == "env"):
            result = failure

    return result or ("test", "找不到失败的 job。")


def classify_rerun(run):
    """Return the classification to record for a previously failed run after a rerun.

    Return None until a rerun succeeds. The caller supplies the updated
    run payload for the earlier failure.
    """
    if isinstance(run, dict) and run.get("conclusion") == "success":
        attempt = run.get("run_attempt")
        if type(attempt) is int and attempt > 1:
            return "env", "CI 重跑后通过。"
    return None


def _is_config_error(name):
    return isinstance(name, str) and _CONFIG_ERROR.match(name.strip()) is not None


def _classify_failed_job(job):
    name = job.get("name")
    steps = job.get("steps")
    if steps == []:
        return "config", "失败的 job 没有任何步骤。"
    if _is_config_error(name):
        return "config", f"失败的 job“{name}”显示工作流解析或 Action 查找失败。"
    if not isinstance(steps, list):
        return "test", "缺少失败 job 的步骤列表。"

    if any(isinstance(step, dict) and step.get("conclusion") == "failure"
           and _is_config_error(step.get("name")) for step in steps):
        return "config", "失败步骤显示工作流解析或 Action 查找失败。"
    if not isinstance(name, str) or not name.strip():
        return "test", "缺少失败 job 的名称。"

    numbers = set()
    failures = []
    tests = []
    for step in steps:
        if not isinstance(step, dict):
            return "test", f"job“{name}”的步骤数据无效。"
        number = step.get("number")
        step_name = step.get("name")
        conclusion = step.get("conclusion")
        if (type(number) is not int or number < 1 or number in numbers
                or not isinstance(step_name, str) or not step_name.strip()
                or conclusion not in ("failure", *_NON_FAILURE)):
            return "test", f"job“{name}”的步骤名称、顺序或结论无法确认。"
        numbers.add(number)
        if conclusion == "failure":
            failures.append((number, step_name))
        if _TEST_STEP.search(step_name):
            tests.append((number, step_name))

    if not failures or not tests:
        return "test", f"job“{name}”缺少可识别的失败步骤或测试类步骤。"

    last_failure, failed_step = max(failures)
    first_test, test_step = min(tests)
    if last_failure < first_test:
        return "env", f"job“{name}”的失败步骤“{failed_step}”在首个测试类步骤“{test_step}”之前。"
    return "test", f"job“{name}”的失败步骤“{failed_step}”位于首个测试类步骤“{test_step}”或之后。"
