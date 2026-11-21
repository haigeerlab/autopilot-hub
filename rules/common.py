"""Shared helpers of the slice-6 rule checks (docs/spec/slice-6.md). Stdlib only; supports Python 3.9."""
import datetime
import json
import re

SPEC = "docs/spec/slice-6.md"
# Token that starts a waiver; `autopr-allow` is the legacy prefix and is still accepted.
ALLOW_TOKEN = r"(?:autopilot|autopr)-allow"
# A line is a waiver attempt when the token follows a comment opener and is not a `<placeholder>` or `{template}` in docs.
ATTEMPT = re.compile(r"(?:#|//|/\*|--|<!--)\s*" + ALLOW_TOKEN + r"\b(?!\s+[<{$])")
WAIVER = re.compile(
    r"(?:#|//|/\*|--|<!--)\s*" + ALLOW_TOKEN + r"\s+(?P<rule>[a-z0-9][a-z0-9_-]*)\s*:\s*(?P<reason>\S.*?)\s+"
    r"until\s+(?P<until>\d{4}-\d{2}-\d{2})\s+approved-by\s+(?P<who>[^\s*>-][^\s*>]*)")


def today_utc():
    return datetime.datetime.now(datetime.timezone.utc).date()


def parse_today(text):
    return datetime.date.fromisoformat(text) if text else today_utc()


def waiver_state(line, rule, today):
    """State of a waiver attempt on `line`: 'ok', 'expired', 'malformed', or None when there is no attempt.

    With a `rule` name, a well-formed waiver for another rule counts as no attempt (None);
    with rule=None every well-formed waiver is judged on its date alone."""
    if not ATTEMPT.search(line):
        return None
    m = WAIVER.search(line)
    if not m:
        return "malformed"
    try:
        until = datetime.date.fromisoformat(m.group("until"))
    except ValueError:
        return "malformed"
    if rule is not None and m.group("rule") != rule:
        return None  # a valid waiver, but for another rule
    return "ok" if until >= today else "expired"


def waived(line, rule, today):
    return waiver_state(line, rule, today) == "ok"


def render(rule, what, why, fix, dont):
    """The six-line error template (skill correction-promotion, step 4)."""
    return "\n".join([
        f"{rule}: {what}",
        f"为什么：{why}",
        f"改成：{fix}",
        f"不要：{dont}",
        f"例外：只能写在本行，格式 `# autopilot-allow {rule}: <理由> until <YYYY-MM-DD> approved-by <人>`",
        f"详见：{SPEC}",
    ])


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def emit(errors, out_md=None, rule=None):
    """Print the errors; write the same text (or a pass line) to out_md. Returns the exit code."""
    text = "\n\n".join(errors) if errors else f"{rule}: 通过"
    print(text)
    if out_md:
        with open(out_md, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if errors else 0
