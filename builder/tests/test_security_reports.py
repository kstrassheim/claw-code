"""Security findings the pull-request reviewer raises.

Four properties carry the feature, and each has failed in a way that looked
like something else:

  1. Findings below the threshold never reach the agent.
  2. A clean result produces NO section at all — which people misread as "the
     scan is broken", so it is asserted explicitly.
  3. An unreadable threshold falls back to the DEFAULT, never to `off`.
     Silently disabling security reporting is the worst way to fail here.
  4. Code scanning being unavailable (403 / 404) is a normal answer, not an
     error that stops the review.

No network: the GitHub client is replaced with one that serves fixtures.
"""

import os
import shutil
import tempfile
import unittest
import urllib.error

from harness import TMP_ROOT, load

sr = load("security_reports")


def findings(*severities):
    return [{"severity": s, "file": "a.py", "line": 1, "name": f"{s} finding",
             "description": "", "identifiers": [], "scanner": "CodeQL"}
            for s in severities]


def alert(severity=None, lint=None, path="a.py", line=1, rule_id="js/x",
          name="a finding", tool="CodeQL"):
    """One code-scanning alert in the shape the API actually returns."""
    rule = {"id": rule_id, "description": name}
    if severity is not None:
        rule["security_severity_level"] = severity
    if lint is not None:
        rule["severity"] = lint
    return {
        "number": 1,
        "state": "open",
        "rule": rule,
        "tool": {"name": tool},
        "most_recent_instance": {
            "ref": "refs/pull/7/head",
            "location": {"path": path, "start_line": line},
            "message": {"text": "something is wrong"},
        },
        "html_url": "https://github.com/o/r/security/code-scanning/1",
    }


class FakeGitHub:
    """Stands in for the API. `raises` is an exception INSTANCE to throw."""

    def __init__(self, alerts=None, raises=None):
        self._alerts = alerts or []
        self._raises = raises
        self.calls = []

    def alerts(self, repo, pr_number):
        self.calls.append((repo, pr_number))
        if self._raises is not None:
            raise self._raises
        return self._alerts


class Ranking(unittest.TestCase):
    def test_critical_is_worst(self):
        self.assertEqual(sr.rank("Critical"), 0)

    def test_case_insensitive(self):
        self.assertEqual(sr.rank("HIGH"), sr.rank("high"))

    def test_unknown_sorts_last_never_first(self):
        # A tool that cannot rate a finding must not be able to push it past a
        # high threshold.
        self.assertGreater(sr.rank("weird"), sr.rank("low"))
        self.assertGreater(sr.rank("unknown"), sr.rank("low"))
        self.assertGreater(sr.rank("info"), sr.rank("low"))


class SeverityMapping(unittest.TestCase):
    """`rule.security_severity_level` is the rating; `rule.severity` is lint."""

    def test_the_security_rating_wins_when_present(self):
        self.assertEqual(
            sr.severity_of({"security_severity_level": "critical",
                            "severity": "note"}), "critical")

    def test_a_lint_error_maps_DOWN_not_up(self):
        # An error-level quality rule is not a high-severity vulnerability.
        # Mapping it up would flood a `high` threshold with lint until the
        # operator turned security reporting off entirely.
        self.assertEqual(sr.severity_of({"severity": "error"}), "medium")
        self.assertGreater(sr.rank(sr.severity_of({"severity": "error"})),
                           sr.rank("high"))

    def test_warnings_and_notes_stay_low_or_below(self):
        self.assertEqual(sr.severity_of({"severity": "warning"}), "low")
        self.assertEqual(sr.severity_of({"severity": "note"}), "info")

    def test_an_unrated_rule_is_unknown(self):
        self.assertEqual(sr.severity_of({}), "unknown")
        self.assertEqual(sr.severity_of(None), "unknown")


class Threshold(unittest.TestCase):
    def setUp(self):
        self.f = findings("critical", "high", "medium", "low", "info", "unknown")

    def test_high_keeps_critical_and_high_only(self):
        self.assertEqual([x["severity"] for x in sr.above(self.f, "high")],
                         ["critical", "high"])

    def test_medium_keeps_three(self):
        self.assertEqual(len(sr.above(self.f, "medium")), 3)

    def test_critical_keeps_one(self):
        self.assertEqual(len(sr.above(self.f, "critical")), 1)

    def test_off_keeps_none(self):
        self.assertEqual(sr.above(self.f, "off"), [])

    def test_unknown_never_passes_a_high_threshold(self):
        self.assertNotIn("unknown",
                         [x["severity"] for x in sr.above(self.f, "high")])

    def test_below_threshold_findings_are_DROPPED_not_marked(self):
        # The whole contract: the agent must never see them, because handed
        # 200 low findings it will write about them.
        out = sr.render(findings("critical", "low"), "high", ["CodeQL"])
        self.assertIn("CRITICAL", out)
        self.assertNotIn("LOW", out)


class Silence(unittest.TestCase):
    def test_nothing_qualifying_produces_no_section(self):
        # The checks are green by then. A "no findings" paragraph in every
        # review teaches the reader to skip the section that will one day
        # matter.
        self.assertEqual(sr.render(findings("low"), "high", ["CodeQL"]), "")

    def test_no_findings_at_all_produces_no_section(self):
        self.assertEqual(sr.render([], "high", ["CodeQL"]), "")

    def test_off_produces_no_section_even_with_criticals(self):
        self.assertEqual(sr.render(findings("critical"), "off", ["CodeQL"]), "")

    def test_a_qualifying_finding_does_produce_one(self):
        out = sr.render(findings("critical"), "high", ["CodeQL"])
        self.assertTrue(out.startswith("## Security findings"))


class Rendering(unittest.TestCase):
    def test_it_tells_the_agent_to_check_against_the_diff(self):
        # Otherwise the reviewer reports pre-existing findings as if the
        # author had introduced them.
        out = sr.render(findings("critical"), "high", ["CodeQL"])
        self.assertIn("introduced it", out)

    def test_it_names_the_file_and_line(self):
        f = findings("critical")
        f[0]["file"], f[0]["line"] = "builder/x.py", 42
        self.assertIn("builder/x.py:42", sr.render(f, "high", ["CodeQL"]))

    def test_a_flood_is_capped_but_the_count_is_honest(self):
        many = findings(*(["critical"] * (sr.MAX_RENDERED + 10)))
        out = sr.render(many, "high", ["CodeQL"])
        self.assertIn(str(len(many)), out.splitlines()[0])
        self.assertIn("omitted", out)
        self.assertEqual(out.count("- **CRITICAL**"), sr.MAX_RENDERED)


class ThresholdFile(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=TMP_ROOT)
        self._orig = sr.LEVEL_FILE
        sr.LEVEL_FILE = os.path.join(self.dir, "level")

    def tearDown(self):
        sr.LEVEL_FILE = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, text):
        with open(sr.LEVEL_FILE, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file_is_the_default(self):
        self.assertEqual(sr.min_severity(), sr.DEFAULT_MIN_SEVERITY)

    def test_a_set_level_is_read(self):
        self._write("# note\nmedium\n")
        self.assertEqual(sr.min_severity(), "medium")

    def test_garbage_falls_back_to_the_DEFAULT_not_to_off(self):
        # The one asymmetry worth stating: quietly disabling security
        # reporting is the worst way to fail here.
        self._write("nonsense\n")
        self.assertEqual(sr.min_severity(), sr.DEFAULT_MIN_SEVERITY)
        self.assertNotEqual(sr.min_severity(), "off")

    def test_an_unreadable_file_falls_back_to_the_DEFAULT(self):
        # A directory where the file should be: open() raises OSError, which
        # is every "cannot read it" case at once.
        os.makedirs(sr.LEVEL_FILE, exist_ok=True)
        self.assertEqual(sr.min_severity(), sr.DEFAULT_MIN_SEVERITY)

    def test_off_is_honoured_when_asked_for_explicitly(self):
        self._write("off\n")
        self.assertEqual(sr.min_severity(), "off")


class Collecting(unittest.TestCase):
    """Alert parsing, with a stand-in for GitHub."""

    def test_alerts_become_findings_sorted_worst_first(self):
        gh = FakeGitHub([alert("high", path="a.js", line=3),
                         alert("critical", path="b.js", line=9)])
        found, tools = sr.collect(gh, "o/r", 7)
        self.assertEqual(tools, ["CodeQL"])
        self.assertEqual([f["severity"] for f in found], ["critical", "high"])
        # Sorted before the cap, so a truncated list keeps the worst.
        self.assertEqual(found[0]["file"], "b.js")

    def test_it_asks_about_the_pull_request_not_the_default_branch(self):
        gh = FakeGitHub([])
        sr.collect(gh, "o/r", 7)
        self.assertEqual(gh.calls, [("o/r", 7)])

    def test_a_403_is_no_findings_not_an_error(self):
        # Advanced Security not enabled, or a token without security_events.
        # A review must still happen and must simply say nothing about
        # scanners.
        gh = FakeGitHub(raises=sr.Unavailable(403))
        self.assertEqual(sr.collect(gh, "o/r", 7), ([], []))

    def test_a_404_is_no_findings_not_an_error(self):
        gh = FakeGitHub(raises=sr.Unavailable(404))
        self.assertEqual(sr.collect(gh, "o/r", 7), ([], []))

    def test_an_unavailable_lookup_renders_as_silence(self):
        gh = FakeGitHub(raises=sr.Unavailable(404))
        found, tools = sr.collect(gh, "o/r", 7)
        self.assertEqual(sr.render(found, "high", tools), "")

    def test_any_other_failure_is_also_survivable(self):
        # A 500, a DNS failure, a malformed body. None of them may stop the
        # review — the security section is an input to a review, not a gate on
        # one.
        gh = FakeGitHub(raises=RuntimeError("GitHub 500"))
        self.assertEqual(sr.collect(gh, "o/r", 7), ([], []))

    def test_a_missing_rating_still_produces_a_finding(self):
        gh = FakeGitHub([alert(None, lint="error")])
        found, _ = sr.collect(gh, "o/r", 7)
        self.assertEqual(found[0]["severity"], "medium")


class HttpClassification(unittest.TestCase):
    """403/404 must become Unavailable inside the client, not RuntimeError."""

    class _Resp:
        def __init__(self, code):
            self.code = code

    def _raise(self, code):
        raise urllib.error.HTTPError("http://x", code, "nope", {}, None)

    def test_403_becomes_unavailable(self):
        gh = sr.GitHub("t")
        gh_open = lambda req, **kw: self._raise(403)  # noqa: E731
        with self.assertRaises(sr.Unavailable):
            self._call(gh, gh_open)

    def test_404_becomes_unavailable(self):
        gh = sr.GitHub("t")
        gh_open = lambda req, **kw: self._raise(404)  # noqa: E731
        with self.assertRaises(sr.Unavailable):
            self._call(gh, gh_open)

    def test_500_is_a_real_error(self):
        gh = sr.GitHub("t")
        gh_open = lambda req, **kw: self._raise(500)  # noqa: E731
        with self.assertRaises(RuntimeError):
            self._call(gh, gh_open)

    def _call(self, gh, opener):
        import urllib.request
        real = urllib.request.urlopen
        urllib.request.urlopen = opener
        try:
            gh.get_json("/repos/o/r/code-scanning/alerts")
        finally:
            urllib.request.urlopen = real


if __name__ == "__main__":
    unittest.main()
