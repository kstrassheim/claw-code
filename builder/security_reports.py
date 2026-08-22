#!/usr/bin/env python3
"""Read the code-scanning alerts raised against a pull request's head, and
render the findings the reviewer should raise.

WHY THE PULL REQUEST'S REF AND NOT THE DEFAULT BRANCH
-----------------------------------------------------
`GET /repos/{owner}/{repo}/code-scanning/alerts` answers for the default
branch unless you tell it otherwise: the state of `main`, not the state of the
change under review. A reviewer quoting that would report problems the author
never introduced and stay silent about the ones they did.

Passing `ref=refs/pull/{n}/head` scopes the answer to THIS pull request's head,
which is what a review is about. GitHub already ran the analysis as part of the
checks the reviewer waited to go green, so nothing is scanned twice and the
severities come back rated rather than needing to be inferred from a log.

WHAT COUNTS AS A SEVERITY
-------------------------
Two fields, in this order:

  rule.security_severity_level   critical / high / medium / low — the security
                                 rating, present on security queries. This is
                                 the one that means what the threshold means.
  rule.severity                  error / warning / note / none — a LINT level,
                                 present on every alert including the purely
                                 stylistic ones.

The fallback deliberately maps DOWN: `error` becomes medium, not high. An
error-level quality rule ("unused variable") is not a high-severity
vulnerability, and mapping it up would flood a `high` threshold with lint until
the operator turned security reporting off — the one outcome this module exists
to prevent.

"Unknown" and "info" sort BELOW low. A tool that cannot rate a finding must not
be able to push it past a high threshold.

SEVERITY IS A THRESHOLD, NOT A FILTER TO TASTE
----------------------------------------------
The operator sets a minimum (critical / high / medium / low, or off) with
`security-level`. Anything below it is dropped before the agent ever sees it —
not shown-but-marked — because a reviewer handed 200 low findings will write
about them, and the point of the threshold is to keep the review about what
matters.

FAILING TO READ THE ALERTS IS NOT AN ERROR
------------------------------------------
Code scanning is not enabled on every repository, and the token may not be able
to read the alerts on one where it is. Both answer 403 or 404, and both mean
exactly "no findings available here" — never "this review cannot happen". A
review whose security lookup failed still reviews the code; it just says
nothing about scanner findings, the same as a clean scan.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# Ordered most severe first; the index IS the rank.
SEVERITIES = ["critical", "high", "medium", "low", "info", "unknown"]

DEFAULT_MIN_SEVERITY = "high"
LEVEL_FILE = os.path.expanduser("~/.openclaw/security-level.conf")

HTTP_TIMEOUT = 30
# A repository with thousands of open alerts is a scanner misconfiguration, not
# a review. Cap what goes into the prompt so one bad run cannot blow the context
# window; the count is always reported in full.
MAX_RENDERED = 40
# One page is the whole budget. The findings are sorted worst-first before the
# cap, so a second page could only ever add things the cap would drop anyway.
PER_PAGE = 100

# The statuses that mean "there are no alerts to read here" rather than "the
# lookup broke": Advanced Security / code scanning not enabled on the repo, or
# a token without the `security_events` scope.
NO_FINDINGS_STATUSES = (403, 404)

# rule.severity is a lint level, not a security rating — see the module
# docstring for why this maps downward.
_LINT_TO_SEVERITY = {
    "error": "medium",
    "warning": "low",
    "note": "info",
    "none": "info",
}


class Unavailable(Exception):
    """No alerts can be read here, and that is a normal answer.

    Carries the HTTP status only so a caller can log which of the two ordinary
    causes it was. Nothing branches on the value.
    """

    def __init__(self, code: int) -> None:
        super().__init__(f"code scanning unavailable (HTTP {code})")
        self.code = code


def rank(sev: str | None) -> int:
    """Lower is worse. Anything unrecognised sorts last, never first."""
    s = (sev or "").strip().lower()
    return SEVERITIES.index(s) if s in SEVERITIES else len(SEVERITIES)


def min_severity() -> str:
    """The configured threshold: a severity name, or "off".

    Falls back to the default on anything unreadable — a missing file, a
    permission error, a line nobody recognises. A broken setting must not
    silently disable security reporting; that is the one failure mode where
    quiet is worst. `off` is honoured only when it is what the file actually
    says.
    """
    try:
        with open(LEVEL_FILE) as f:
            for line in f:
                line = line.split("#", 1)[0].strip().lower()
                if not line:
                    continue
                if line in SEVERITIES or line == "off":
                    return line
                # A line that says something else is a corrupt setting, not a
                # comment to skip past. Stop and take the default.
                break
    except OSError:
        pass
    return DEFAULT_MIN_SEVERITY


def severity_of(rule: dict | None) -> str:
    """The severity of one alert's rule: the security rating when it has one,
    otherwise the lint level mapped down (see the module docstring)."""
    r = rule or {}
    sec = (r.get("security_severity_level") or "").strip().lower()
    if sec in SEVERITIES:
        return sec
    lint = (r.get("severity") or "").strip().lower()
    return _LINT_TO_SEVERITY.get(lint, "unknown")


class GitHub:
    """The three headers every other runner in this image sends, and one rule:
    403/404 is `Unavailable`, not an error."""

    def __init__(self, token: str, api: str = "https://api.github.com") -> None:
        self.api = (api or "https://api.github.com").rstrip("/")
        self.token = token

    def get_json(self, path: str):
        req = urllib.request.Request(
            self.api + path,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "security-reports/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in NO_FINDINGS_STATUSES:
                raise Unavailable(e.code) from None
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise RuntimeError(f"GitHub {e.code} on {path}: {body[:200]}") from None

    def alerts(self, repo: str, pr_number: int) -> list:
        """Open code-scanning alerts on this pull request's head."""
        ref = urllib.parse.quote(f"refs/pull/{pr_number}/head", safe="")
        data = self.get_json(
            f"/repos/{repo}/code-scanning/alerts"
            f"?ref={ref}&state=open&per_page={PER_PAGE}"
        )
        return data if isinstance(data, list) else []


def collect(gh, repo: str, pr_number: int) -> tuple[list[dict], list[str]]:
    """Return (findings, tools-seen). Never raises.

    A review must still happen when the alerts cannot be read — code scanning
    off, no permission, GitHub having a bad minute — and it simply says nothing
    about scanners in that case, exactly as it does for a clean scan.
    """
    try:
        alerts = gh.alerts(repo, pr_number)
    except Unavailable:
        return ([], [])
    except Exception:
        return ([], [])

    findings: list[dict] = []
    tools: list[str] = []
    for a in alerts if isinstance(alerts, list) else []:
        if not isinstance(a, dict):
            continue
        rule = a.get("rule") or {}
        tool = (a.get("tool") or {}).get("name") or "code scanning"
        tools.append(tool)
        inst = a.get("most_recent_instance") or {}
        loc = inst.get("location") or {}
        message = (inst.get("message") or {}).get("text") or ""
        findings.append({
            "scanner": tool,
            "severity": severity_of(rule),
            "name": rule.get("description") or rule.get("name")
                    or message or "(unnamed finding)",
            "file": loc.get("path") or "",
            "line": loc.get("start_line"),
            "description": (message or rule.get("full_description") or "").strip(),
            "identifiers": [i for i in (rule.get("id"), a.get("number")) if i],
            "url": a.get("html_url") or "",
        })
    findings.sort(key=lambda f: (rank(f["severity"]), f["file"], f["line"] or 0))
    return (findings, sorted(set(tools)))


def above(findings: list[dict], threshold: str) -> list[dict]:
    if threshold == "off":
        return []
    limit = rank(threshold)
    return [f for f in findings if rank(f["severity"]) <= limit]


def render(findings: list[dict], threshold: str, tools: list[str]) -> str:
    """The section handed to the reviewing agent, or "" for silence.

    Silence is the whole contract when nothing qualifies: the checks are
    already green by then, so a "no security findings" paragraph in every
    review is noise that trains the reader to skip the section that will one
    day matter.
    """
    kept = above(findings, threshold)
    if not kept:
        return ""
    shown = kept[:MAX_RENDERED]
    lines = [
        f"## Security findings from code scanning ({len(kept)} at {threshold} or above)",
        "",
        f"Tools that reported: {', '.join(tools) or 'unknown'}. These alerts were",
        "raised against THIS pull request's head, so they describe this branch.",
        "Raise them in your review: confirm each one against the diff, say whether",
        "this pull request introduced it, and leave it alone if it is pre-existing",
        "and out of scope — say that too.",
        "",
    ]
    for f in shown:
        where = f["file"] + (f":{f['line']}" if f["line"] else "")
        idents = [str(i) for i in f["identifiers"][:2]]
        ident = f" [{', '.join(idents)}]" if idents else ""
        lines.append(f"- **{f['severity'].upper()}** {f['name']}{ident}")
        if where:
            lines.append(f"  - `{where}`")
        if f["description"]:
            d = " ".join(f["description"].split())
            lines.append(f"  - {d[:300]}")
    if len(kept) > len(shown):
        lines.append("")
        lines.append(
            f"({len(kept) - len(shown)} further findings at or above {threshold} "
            "were omitted from this list to keep the prompt bounded — say so if "
            "you summarise the count.)"
        )
    return "\n".join(lines)


def main() -> int:
    import sys

    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    threshold = min_severity()

    if threshold == "off":
        return 0
    if not (repo and pr_number and token):
        return 0
    try:
        findings, tools = collect(GitHub(token, api), repo, int(pr_number))
    except Exception:
        return 0
    out = render(findings, threshold, tools)
    if out:
        sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
