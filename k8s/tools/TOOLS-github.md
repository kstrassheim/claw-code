<!--
  GitHub-specific bits ONLY. The provider-agnostic workflow rules
  (mantra, ABSOLUTE rule, Steps 1–7, stale-branch cleanup,
  read-and-react) live in TOOLS-gitflow.md — read that first.
  This file just translates those steps to the GitHub MCP /
  CLI / auth specifics on this pod.
-->

---

# GitHub — autonomous coding access

You have a **dedicated GitHub bot account** wired into this pod
for autonomous coding work. Read TOOLS-gitflow.md for the workflow
rules; this doc only covers the GitHub-specific tools, auth, and
shortcuts.

## What is set up for you

- **`GITHUB_TOKEN`** — bot account's classic PAT (`repo`,
  `workflow`, `read:org`), exposed via the pod's env from the
  sealed `openclaw-secrets` Secret. The token is referenced by
  the baked-in `~/.gitconfig` credential helper so
  `git push https://github.com/...` works even though the
  `exec` tool strips `GITHUB_TOKEN` from the child env.
- **`/usr/local/bin/gh`** — GitHub CLI. Auto-authenticates via
  `GITHUB_TOKEN`. ⚠️ `gh` calls from `exec` may see no token
  because the exec sandbox strips `GITHUB_TOKEN` — prefer the
  MCP path below when you can.
- **`mcp.servers.github`** — official `github-mcp-server`. The
  gateway spawns it with the token in its child env, so MCP
  tool calls authenticate cleanly. Use `listTools` against this
  MCP to see the full surface — naming shifts between versions.
  Typical capabilities you can expect:
    - issue / PR / branch / repo CRUD (search, get, create,
      merge, delete_ref)
    - reactions on issues, issue comments, and PR review
      comments
    - workflow-run + job-log fetching for reading CI failures
    - `request_copilot_review` to request the auto reviewer
- **`git`** — `~/.gitconfig` is baked into the image with a
  credential helper that reads `$GITHUB_TOKEN` at runtime, so
  HTTPS clone / push / fetch all work without a manual login.

## Scope of access

- All repos the bot account itself **owns**.
- All repos where the bot account has been added as a
  **collaborator** — including any org where the human user has
  invited the bot.
- The bot can **NOT** see private repos it has not been invited
  to. If a `gh repo view` / `get_file_contents` returns 404,
  that's normal — the user just hasn't added the bot as
  collaborator yet; ask them to do it.

## Working copies under `~/.openclaw/workspace/scratch/`

Clone working copies into a per-owner scratch dir, then push back
to origin. Origin URLs should always be HTTPS (so the credential
helper picks up `$GITHUB_TOKEN`):

```bash
mkdir -p ~/.openclaw/workspace/scratch/<owner>
git clone "https://github.com/<owner>/<repo>.git" \
  ~/.openclaw/workspace/scratch/<owner>/<repo>
cd ~/.openclaw/workspace/scratch/<owner>/<repo>
git fetch origin
```

Whether `git push` actually works depends on the bot's access:

- Collaborator → push works via the credential helper.
- Read-only access → `git push` returns 403; deliver as
  "analyze and comment" instead. Don't brute-force.

Disk budget: the workspace PVC is shared with all other pod
state. Clone only what you're working on; `rm -rf` scratch
dirs when done.

## GitHub-specific translations of TOOLS-gitflow.md steps

The official `github-mcp-server` exposes the tools listed below
(names may differ slightly across versions — use `listTools` if
you get a tool-not-found error):

| Generic step                        | GitHub tool                                                                                  |
|-------------------------------------|----------------------------------------------------------------------------------------------|
| Step 1: find assigned issues        | `mcp.servers.github.search_issues query="assignee:@me is:issue is:open repo:<O>/<R>"`        |
| Step 1: re-verify issue state       | `mcp.servers.github.get_issue`                                                               |
| Step 1.5: react 👍 to a comment      | `mcp.servers.github.add_issue_comment_reaction` (or the PR-review-comment equivalent)        |
| Step 4: open PR                     | `mcp.servers.github.create_pull_request base=main head=feature/issue-<n>`                    |
| Step 4: poll CI                     | `mcp.servers.github.get_pull_request_status` / `list_workflow_runs`                          |
| Step 4: read failing job log        | `mcp.servers.github.list_workflow_runs` → `list_workflow_jobs` → `get_job_logs`              |
| Step 4: request reviewer            | `mcp.servers.github.request_copilot_review`                                                  |
| Step 5: address review              | `mcp.servers.github.list_pull_request_reviews` + `add_issue_comment`                         |
| Step 6: squash merge                | `mcp.servers.github.merge_pull_request merge_method="squash"`                                |
| Step 6: delete remote branch        | `mcp.servers.github.delete_ref ref="heads/feature/issue-<n>"`                                |
| Step 2.5: list bot's branches       | `mcp.servers.github.list_branches` + filter to your login                                    |

## Quick reference

| Goal                            | Command                                                              |
|---------------------------------|----------------------------------------------------------------------|
| Resolve own bot login           | `mcp.servers.github.get_me` (or `gh api user` from a non-exec shell) |
| List bot's visible repos        | `gh repo list --limit 100` *(only from a non-exec shell)*            |
| Clone over HTTPS                | `git clone https://github.com/<owner>/<repo>.git ~/.openclaw/workspace/scratch/<owner>/<repo>` |
| Read CI log directly            | `mcp.servers.github.get_job_logs job_id=<n>`                         |
| API rate budget                 | `gh api rate_limit --jq .resources.core` *(non-exec shell)*          |
