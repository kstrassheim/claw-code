<!--
  Describes the security scanners baked into the image and their MCP
  surfaces (semgrep MCP + in-house security-mcp). Written primarily for
  the MR-reviewer role, but every agent may use them for CHECKING code.
-->

---

# Security scanning — `mcp.servers.semgrep` + `mcp.servers.security`

You have two MCP servers dedicated to security CHECKS (never fixes —
they are all read-only scanners):

- **`mcp.servers.semgrep`** — the official Semgrep MCP server.
  Multi-language SAST: Python/FastAPI, JavaScript/TypeScript, React,
  and more. Use its scan tools with security-focused rulesets
  (`p/security-audit`, `p/owasp-top-ten`, `p/python`, `p/react`,
  `p/javascript`, `p/secrets`). The `semgrep` CLI is also on PATH if
  you prefer `semgrep scan --config p/security-audit --json <path>`.
- **`mcp.servers.security`** — in-house wrapper around the remaining
  scanners (see tool table below).

## When to scan (MR review / code check workflow)

Scan the CHANGED code, not the world. Work out the changed files
(`git diff --name-only origin/<target>...HEAD`) and route each to the
scanners that apply:

| Changed code | Run |
|---|---|
| Python / FastAPI (`*.py`) | semgrep (`p/python` + `p/security-audit`), `security.bandit_scan` on the package dir |
| `requirements*.txt` / Python deps | `security.pip_audit` on the requirements file |
| JS / TS / React (`*.js`, `*.jsx`, `*.ts`, `*.tsx`) | semgrep (`p/react`, `p/javascript`, `p/security-audit`) |
| `package.json` / lockfile | `security.npm_audit` in that directory |
| PowerShell (`*.ps1`, `*.psm1`) | `security.psscriptanalyzer_scan` |
| ANY change | `security.gitleaks_scan` on the checkout (hardcoded secrets in the tree) |
| ANY branch review | `security.gitleaks_git_scan` with `log_opts: "origin/<target>..HEAD"` (secrets in the branch's COMMIT HISTORY) |

## MCP tool surface (`mcp.servers.security.*`)

| Tool | Purpose |
|---|---|
| `bandit_scan` | Python SAST (injection, weak crypto, subprocess misuse, hardcoded passwords). `path` = dir or file; optional `severity`/`confidence` floors. JSON findings. |
| `pip_audit` | Known CVEs in Python dependencies. `requirements` = path to requirements.txt. JSON. |
| `npm_audit` | Known CVEs in JS dependencies (`npm audit --json`). `cwd` = dir with package.json + lockfile; optional `omit_dev`. |
| `psscriptanalyzer_scan` | PowerShell static analysis incl. security rules. `path` = .ps1/.psm1 or dir; optional `severity` list. JSON diagnostics. |
| `gitleaks_scan` | Hardcoded secrets / API keys / private keys in a working tree. `path` = checkout dir. JSON findings, empty = clean. Blind to secrets that were committed and deleted again. |
| `gitleaks_git_scan` | Secrets in GIT HISTORY (`gitleaks git`). `path` = repo root; `log_opts` restricts the range — use `origin/<target>..HEAD` to scan exactly the commits an MR adds. Catches committed-then-'removed' credentials, which still ship in history on merge. A hit means: rewrite the branch history AND rotate the credential — deleting the file in a new commit is NOT a fix. |

Scanners exit non-zero when they FIND things — the MCP reports that as
a successful scan with findings, so a non-empty result is data, not an
error.

## Reporting policy — signal, not noise

- Report findings **on the changed code**. A pre-existing problem in
  untouched code is only worth mentioning when it is severe — and then
  explicitly label it *pre-existing*, never as a blocker for the
  change under review.
- Deduplicate: semgrep and bandit overlap on Python — one finding, one
  report line.
- Dependency-audit results (pip-audit / npm audit) count as blockers
  only when the MR **introduced or bumped** the vulnerable dependency;
  otherwise they are pre-existing.
- For every finding you report, give: file:line, the rule/CVE id, why
  it matters here, and the concrete change that fixes it.
- NEVER paste raw scanner JSON into an issue/MR note — summarize.

## CLI quick reference (fallback when MCP tools are unavailable)

```bash
semgrep scan --config p/security-audit --config p/secrets --json <dir>
bandit -r <dir> -f json -q -x venv,.venv,node_modules
pip-audit -r requirements.txt -f json
cd <frontend-dir> && npm audit --json
pwsh -NoProfile -Command "Invoke-ScriptAnalyzer -Path <dir> -Recurse | ConvertTo-Json -Depth 4"
gitleaks dir <dir> --no-banner --report-format json --report-path /dev/stdout
gitleaks git <repo> --log-opts 'origin/main..HEAD' --no-banner --report-format json --report-path /dev/stdout
```
