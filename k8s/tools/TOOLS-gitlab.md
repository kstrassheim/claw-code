<!--
  GitLab-specific bits ONLY. The provider-agnostic workflow rules
  (mantra, ABSOLUTE rule, Steps 1–7, stale-branch cleanup,
  read-and-react) live in TOOLS-gitflow.md — read that first.
  This file just translates those steps to the GitLab MCPs +
  glab CLI on this pod.
-->

---

# GitLab — `glab` CLI + `mcp.servers.gitlab_*` instances

GitLab access is provided via the `@yoda.digital/gitlab-mcp-server`
MCP package. Up to two MCP instances of the same package can be
configured, each targeting a different host:

| MCP                          | Targets                              | Env vars                                            |
|------------------------------|--------------------------------------|-----------------------------------------------------|
| `mcp.servers.gitlab_cloud`   | `https://gitlab.com`                 | `GITLAB_PERSONAL_ACCESS_TOKEN=$GITLAB_TOKEN`        |
| `mcp.servers.gitlab_local`   | `$GITLAB_LOCAL_URL` (self-hosted)    | `GITLAB_PERSONAL_ACCESS_TOKEN=$GITLAB_LOCAL_TOKEN`  |

Read TOOLS-gitflow.md for the workflow rules; this doc only
covers GitLab-specific tools, auth, and shortcuts.

## ⚠️ State: GitLab support is documented but NOT wired in this image

In the current claw-code build, **neither** the `glab` CLI nor the
GitLab MCP servers are baked into the image, and no GitLab tokens
are sealed into `openclaw-secrets`. This document describes the
target shape of GitLab support — it tells you what the MCP names
will be once the build wires them, and what envs you will need.

Until the Dockerfile installs `glab` + `@yoda.digital/gitlab-mcp-server`
AND a human seals the secrets below, **don't pretend the bot can
act on GitLab.** Surface the unconfigured state and ask the user to
provision the binaries + PATs.

Secrets you will need (once wired):

| Secret              | Purpose                                          |
|---------------------|--------------------------------------------------|
| `GITLAB_TOKEN`      | PAT for gitlab.com bot account (scopes: `api`, `read_api`, `read_repository`, `write_repository`) |
| `GITLAB_LOCAL_TOKEN`| PAT for a self-hosted bot account, same scopes  |
| `GITLAB_LOCAL_URL`  | Base URL of the self-hosted instance, e.g. `https://gitlab.<your-domain>` (no trailing slash) |

The `gitlab_local` MCP + the two `*_LOCAL_*` envs are optional —
omit them if you only need gitlab.com access.

## Picking which MCP to use

Look at the project's URL or the issue's URL:

- `https://gitlab.com/<owner>/<project>` → **`mcp.servers.gitlab_cloud`**
- `<GITLAB_LOCAL_URL>/<owner>/<project>` → **`mcp.servers.gitlab_local`**

If unsure which one the user means, **ask before guessing**.
Calling the wrong MCP returns `404 Project Not Found` — silent
failure for you, confusing for the user.

## Pipeline + job log reading (the critical capability)

The whole reason the MCP is wired the way it is: you can read
pipeline run results from MRs end-to-end. When a merge request's
CI is red, walk it through using the MCP's pipeline tools
(`listTools` for exact names — naming can shift between package
versions; expected pattern below):

1. **List pipelines on the MR's source branch** —
   `list_pipelines project_id=<n> ref="feature/issue-<n>"`.
   Pick the latest by `created_at` DESC.
2. **List jobs in that pipeline** —
   `list_pipeline_jobs project_id=<n> pipeline_id=<m>`. Filter
   to `status=failed`.
3. **Fetch the job log (trace)** —
   `get_job_log` / `get_job_trace project_id=<n> job_id=<k>`.
   Returns raw text. Parse the error, fix the code, push,
   watch the next pipeline.

## Reactions (👍 = "I read & understood")

GitLab calls reactions "award emoji". The MCP exposes them per
note id:

- `mcp.servers.gitlab_*.award_emoji` to add 👍 to an issue note
  or MR note.
- Check existing awards first to avoid duplicates and avoid
  self-thumbs (same rule as TOOLS-gitflow.md Step 1.5).
- Bot's own login resolves via the MCP's `get_me` /
  `current_user` tool (or `glab api user`).

## GitLab-specific translations of TOOLS-gitflow.md steps

`<cloud|local>` = whichever MCP suffix matches the project's host.

| Generic step                  | GitLab tool                                                                                    |
|-------------------------------|------------------------------------------------------------------------------------------------|
| Step 1: find assigned issues  | `mcp.servers.gitlab_<cloud|local>.list_issues assignee_id=<me> state="opened"`                 |
| Step 1: re-verify state       | `mcp.servers.gitlab_*.get_issue project_id=<n> issue_iid=<i>`                                  |
| Step 1.5: react 👍             | `mcp.servers.gitlab_*.award_emoji` on the note id                                              |
| Step 4: open MR               | `mcp.servers.gitlab_*.create_merge_request source_branch=feature/issue-<n> target_branch=main` |
| Step 4: poll pipeline         | `list_pipelines ref=feature/issue-<n>` → `list_pipeline_jobs`                                  |
| Step 4: read failing job log  | `get_job_log` (or `get_job_trace`) `job_id=<k>`                                                |
| Step 5: address review        | `list_merge_request_notes` + `create_merge_request_note`                                       |
| Step 6: squash-merge          | `mcp.servers.gitlab_*.accept_merge_request squash=true`                                        |
| Step 6: delete branch         | `mcp.servers.gitlab_*.delete_branch project_id=<n> branch="feature/issue-<n>"`                 |
| Step 2.5: list bot's branches | `mcp.servers.gitlab_*.list_branches project_id=<n>` + filter to your username                  |

## Working copies

Clone source for GitLab projects depends on the host:

- Cloud: `git clone https://x-access-token:$GITLAB_TOKEN@gitlab.com/<owner>/<repo>.git ~/.openclaw/workspace/scratch/<owner>/<repo>`
- Self-hosted: substitute `$GITLAB_LOCAL_TOKEN` + base URL

The git credential helper at the pod level only carries the
GitHub token. For GitLab pushes from `exec` (which strips
`GITLAB_TOKEN`), include the token in the remote URL as shown
above OR run `glab auth login --hostname <host> --token <pat>`
once per host to populate `~/.config/glab-cli/`.

## glab CLI fallback

Prefer the MCP. Reach for `glab` when:

- One-off shell pipe with `jq` projection.
- Bulk `glab repo clone --group <name>` style ops.
- Authenticating to a new self-hosted host once
  (`glab auth login --hostname <host>`).

⚠️ `glab` from `exec` may fail with "no token" because the exec
sandbox strips `GITLAB_TOKEN`. Use the MCP for any API call
that hits the network.

## Quick reference

| Goal                                | How                                                                            |
|-------------------------------------|--------------------------------------------------------------------------------|
| Resolve own login (cloud)           | `mcp.servers.gitlab_cloud.get_me`                                              |
| List my open MRs (cloud)            | `mcp.servers.gitlab_cloud.list_merge_requests assignee_id=<me> state="opened"` |
| Get pipeline status for MR          | `mcp.servers.gitlab_<cloud|local>.list_pipelines ref=<branch>`                 |
| Read failing job log                | `mcp.servers.gitlab_<cloud|local>.get_job_log job_id=<k>`                      |
| Comment on MR                       | `mcp.servers.gitlab_*.create_merge_request_note`                               |
| Merge MR (when CI green)            | `mcp.servers.gitlab_*.accept_merge_request squash=true`                        |
| Cross-instance shell op             | `glab --hostname <host> <subcommand>`                                          |
