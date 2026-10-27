<!--
  GitLab-specific bits ONLY. The provider-agnostic workflow rules
  (mantra, ABSOLUTE rule, Steps 1–7, stale-branch cleanup,
  read-and-react) live in TOOLS-gitflow.md — read that first.
  This file just translates those steps to the GitLab MCPs +
  glab CLI on this pod.
-->

---

# GitLab — `glab` CLI + `mcp.servers.gitlab_local`

GitLab access is the on-prem instance the bot already works on — the one in
`$GITLAB_URL` (the same instance its issues and merge requests live on). Two
tools reach it, both baked into the image and both authenticated as the bot:

| Tool                        | How it is wired                                                         |
|-----------------------------|-------------------------------------------------------------------------|
| `mcp.servers.gitlab_local`  | `@yoda.digital/gitlab-mcp-server`, pointed at the API of `$GITLAB_URL` with `$GITLAB_API_TOKEN` |
| `glab` CLI                  | reads `GITLAB_HOST` / `GITLAB_TOKEN` from the pod environment (same instance, same token) |

All four variables come from `claw-code-secrets`, written by the deploy from
the `GITLAB_API_TOKEN` / `GITLAB_URL` repository secrets. If either is unset,
render-config removes `gitlab_local` at pod start — then there is no GitLab MCP
and you must say so rather than guess.

There is **no gitlab.com access** — no MCP and no token for it. A
`https://gitlab.com/...` project is out of reach; tell the user.

Read TOOLS-gitflow.md for the workflow rules; this doc only
covers GitLab-specific tools, auth, and shortcuts.

## Pipeline + job log reading (the critical capability)

The whole reason the MCP is wired the way it is: you can read
pipeline run results from MRs end-to-end. When a merge request's
CI is red, walk it through using the MCP's pipeline tools
(`listTools` for exact names — naming can shift between package
versions; expected pattern below):

1. **List pipelines on the MR's source branch** —
   `list_pipelines project_id=<n> ref="<n>-<slug>"`.
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

- `mcp.servers.gitlab_local.award_emoji` to add 👍 to an issue note
  or MR note.
- Check existing awards first to avoid duplicates and avoid
  self-thumbs (same rule as TOOLS-gitflow.md Step 1.5).
- Bot's own login resolves via the MCP's `get_me` /
  `current_user` tool (or `glab api user`).

## GitLab-specific translations of TOOLS-gitflow.md steps

| Generic step                  | GitLab tool                                                                                    |
|-------------------------------|------------------------------------------------------------------------------------------------|
| Step 1: find assigned issues  | `mcp.servers.gitlab_local.list_issues assignee_id=<me> state="opened"`                 |
| Step 1: re-verify state       | `mcp.servers.gitlab_local.get_issue project_id=<n> issue_iid=<i>`                                  |
| Step 1.5: react 👍             | `mcp.servers.gitlab_local.award_emoji` on the note id                                              |
| Step 4: open MR               | `mcp.servers.gitlab_local.create_merge_request source_branch=<n>-<slug> target_branch=main` |
| Step 4: poll pipeline         | `list_pipelines ref=<n>-<slug>` → `list_pipeline_jobs`                                  |
| Step 4: read failing job log  | `get_job_log` (or `get_job_trace`) `job_id=<k>`                                                |
| Step 5: address review        | `list_merge_request_notes` + `create_merge_request_note`                                       |
| Step 6: squash-merge          | `mcp.servers.gitlab_local.accept_merge_request squash=true`                                        |
| Step 6: delete branch         | `mcp.servers.gitlab_local.delete_branch project_id=<n> branch="<n>-<slug>"`                 |
| Step 2.5: list bot's branches | `mcp.servers.gitlab_local.list_branches project_id=<n>` + filter to your username                  |

**Branch naming — this is how GitLab links a branch to its issue.** Name the
branch `<issue-iid>-<short-slug>` (e.g. `42-disable-the-send-button`). GitLab
keys its **Related branches** section off the **leading issue number** and
nothing else, so a branch that starts with the iid shows up on the issue as
soon as it is pushed, and the merge request opened from it inherits that link.
A name that does not start with the number — `feature/issue-<n>`,
`issue-<n>-fix` — is never linked, no matter what it says afterwards. **This
overrides the generic `feature/issue-<number>` rule in the gitflow section
above: on GitLab, and only on GitLab, the number comes first.** Put
`Closes #<iid>` in the MR description as well, so the merge closes the issue.

## Working copies

Clone over HTTPS with the bot's token in the URL (`$GITLAB_HOST` is the base
URL, e.g. `https://gitlab.example.org`):

- `git clone "https://oauth2:${GITLAB_TOKEN}@${GITLAB_HOST#https://}/<group>/<repo>.git" ~/.openclaw/workspace/scratch/<group>/<repo>`

The git credential helper at the pod level only carries the
GitHub token. For GitLab pushes from `exec` (which strips
`GITLAB_TOKEN`), include the token in the remote URL as shown
above OR run `glab auth login --hostname <host> --token <pat>`
once to populate `~/.config/glab-cli/`.

## glab CLI fallback

Prefer the MCP. Reach for `glab` when:

- One-off shell pipe with `jq` projection.
- Bulk `glab repo clone --group <name>` style ops.
- Authenticating once from a shell that lacks `GITLAB_TOKEN`
  (`glab auth login --hostname <host>`).

⚠️ `glab` from `exec` may fail with "no token" because the exec
sandbox strips `GITLAB_TOKEN`. Use the MCP for any API call
that hits the network.

## Quick reference

| Goal                                | How                                                                            |
|-------------------------------------|--------------------------------------------------------------------------------|
| Resolve own login                   | `mcp.servers.gitlab_local.get_me`                                              |
| List my open MRs                    | `mcp.servers.gitlab_local.list_merge_requests assignee_id=<me> state="opened"` |
| Get pipeline status for MR          | `mcp.servers.gitlab_local.list_pipelines ref=<branch>`                 |
| Read failing job log                | `mcp.servers.gitlab_local.get_job_log job_id=<k>`                      |
| Comment on MR                       | `mcp.servers.gitlab_local.create_merge_request_note`                               |
| Merge MR (when CI green)            | `mcp.servers.gitlab_local.accept_merge_request squash=true`                        |
| Shell op with `jq`                  | `glab api <path> \| jq ...`                                                     |
