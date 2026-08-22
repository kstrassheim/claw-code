"""The solver must be able to read WHY CI is red, not just that it is.

`fixer-runner` pre-fetches the failing job's log and injects it into the
agent's prompt under "## Failing CI excerpt", precisely so the agent does not
have to remember to go and get it. That pre-fetch returned "" every time, on
both hosts, for two independent reasons — and `except Exception: return ""`
hid both. What reached the agent was an empty section, so it reasoned from
component code alone and said it could not fetch the log. It was right.

    1. GitHub 302s /actions/jobs/<id>/logs to blob storage. urllib replays
       every header across a redirect, so the GitHub token went to Azure,
       which answered 403 AuthenticationFailed. curl drops the header on a
       cross-host redirect, which is why fetching by hand looked fine.
    2. Job logs are plain text. The shared reader ends in json.loads() and
       answers None on a ValueError, so even a 200 became "".
"""

import unittest
import urllib.parse
import urllib.request

from harness import load  # noqa: F401 - puts builder/ on sys.path

import forge  # noqa: E402


class TheCredentialStopsAtTheHostBoundary(unittest.TestCase):
    def redirect_to(self, target, url="https://api.github.com/x/logs"):
        req = urllib.request.Request(
            url, headers={"Authorization": "token abc",
                          "Private-Token": "glpat-abc",
                          "Accept": "application/json"})
        return forge._StripAuthAcrossHosts().redirect_request(
            req, None, 302, "Found", {}, target)

    def test_a_cross_host_redirect_drops_the_credential(self):
        new = self.redirect_to("https://productionresults.blob.core.windows.net/x?sig=y")
        self.assertIsNotNone(new)
        self.assertIsNone(new.get_header("Authorization"))
        self.assertIsNone(new.get_header("Private-token"))

    def test_headers_that_are_not_credentials_survive(self):
        # Dropping everything would break content negotiation on the target.
        new = self.redirect_to("https://blob.example.com/x")
        self.assertEqual(new.get_header("Accept"), "application/json")

    def test_a_same_host_redirect_keeps_it(self):
        # An ordinary API redirect must stay authenticated, or it 401s.
        new = self.redirect_to("https://api.github.com/x/logs/final")
        self.assertEqual(new.get_header("Authorization"), "token abc")

    def test_the_host_comparison_ignores_case(self):
        new = self.redirect_to("https://API.GitHub.com/x/logs/final")
        self.assertEqual(new.get_header("Authorization"), "token abc")


class TheLogComesBackAsText(unittest.TestCase):
    """`raw=True` is what keeps a log from being parsed into nothing."""

    def github(self, routes):
        import test_forge_github as tfg
        t = tfg.FakeTransport(routes)
        return forge.GitHubForge("token", transport=t), t

    def test_the_failing_jobs_log_is_returned(self):
        f, t = self.github({
            "check-runs": {"check_runs": [
                {"name": "e2e", "conclusion": "failure", "id": 42,
                 "app": {"slug": "github-actions"}}]},
            "/actions/jobs/42/logs": "AssertionError: expected true\nexit 1\n",
        })
        self.assertIn("AssertionError",
                      f.failing_check_log("o/r", "abc1234"))

    def test_the_log_fetch_asks_for_text_not_json(self):
        # The regression that made this empty: the shared reader json-parses
        # and answers None, so the log was discarded on the way back.
        f, t = self.github({
            "check-runs": {"check_runs": [
                {"name": "e2e", "conclusion": "failure", "id": 42,
                 "app": {"slug": "github-actions"}}]},
            "/actions/jobs/42/logs": "boom",
        })
        f.failing_check_log("o/r", "abc1234")
        self.assertTrue(any(t.raw_flags), "the log fetch must pass raw=True")

    def test_only_the_tail_is_kept(self):
        f, _ = self.github({
            "check-runs": {"check_runs": [
                {"name": "e2e", "conclusion": "failure", "id": 42,
                 "app": {"slug": "github-actions"}}]},
            "/actions/jobs/42/logs": "x" * 100 + "THE-END",
        })
        got = f.failing_check_log("o/r", "abc1234", limit=10)
        self.assertEqual(got, "THE-END"[-10:] if len("THE-END") > 10 else
                         ("x" * 100 + "THE-END")[-10:])

    def test_a_green_commit_has_no_log(self):
        f, _ = self.github({
            "check-runs": {"check_runs": [
                {"name": "e2e", "conclusion": "success", "id": 42,
                 "app": {"slug": "github-actions"}}]},
        })
        self.assertEqual(f.failing_check_log("o/r", "abc1234"), "")

    def test_a_failing_check_from_another_app_is_not_guessed_at(self):
        # Only this host's own runs have a job log at that id.
        f, _ = self.github({
            "check-runs": {"check_runs": [
                {"name": "scanner", "conclusion": "failure", "id": 42,
                 "app": {"slug": "some-scanner"}}]},
        })
        self.assertEqual(f.failing_check_log("o/r", "abc1234"), "")


if __name__ == "__main__":
    unittest.main()
