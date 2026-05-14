<!--
  Provider-agnostic git / PR workflow rules. Applies identically to
  every git host you have access to (GitHub on github.com, GitLab on
  gitlab.com, and self-hosted GitLab if configured). Provider-specific
  bits — auth env vars, MCP tool names, CI workflow filenames,
  reaction APIs — live in TOOLS-github.md / TOOLS-gitlab.md.

  Translation table for reading this doc against your target host:

    GitHub                       GitLab
    ------                       ------
    pull request (PR)            merge request (MR)
    `feature/issue-<n>`          `feature/issue-<n>`           (same)
    `Closes #<n>` (body)         `Closes #<n>` (description)   (same)
    CodeQL (default check)       SAST / your .gitlab-ci.yml
    `request_copilot_review`     `approve_merge_request` (etc.)
    add_issue_comment_reaction   award_emoji (👍)
-->

---

# git-flow rules (apply to every host you push to)

## ⛔ THE MANTRA: **1 issue = 1 branch = 1 pull/merge request**

The "1" is literal in all three directions. EXACTLY one issue per
branch — NOT "two related issues sharing a branch", NOT
`feature/issue-A-B-...` combining them. Each open assigned issue
gets its own dedicated cycle through Steps 2.5–7. Period.

- No sub-branches. No parallel branches. No parallel PRs/MRs. Ever.
- **No combined branches either** — if you're tempted to name a
  branch `feature/issue-X-Y` or `feature/issues-A-B-C` because
  the issues "seem related", STOP. Pick the older issue, work
  it alone in `feature/issue-<n>`, merge it, then start
  `feature/issue-<m>` for the next one.
- You finish an issue COMPLETELY (through PR/MR merge + branch
  delete) before touching the next issue.
- Branch name is **exactly** `feature/issue-<number>` — no
  `-clean`, `-v2`, `-fix`, `-rebase`, `-and-<other>`,
  `-with-<feature>`, `-X-Y` variants. If something's wrong on
  the branch, fix it on that branch — don't open a sibling.
- ONE PR/MR per repo at a time. **Never open a second** while one
  is already open in the same repo.
- A new branch is only created from a **FRESHLY-ADVANCED `main`**
  (or whatever the project's default branch is named):
  ```
  # previous PR/MR has just been merged
  git checkout main && git pull origin main   # main advances
  git checkout -b feature/issue-<next>        # branch off NEW main
  ```
  If `main` hasn't advanced since your last branch was created,
  there's no new merge point — keep iterating on the open one.

## ⛔ ABSOLUTE rule — NEVER bypass branch protection / CI / reviews

Branch protection rules, required CI checks, and required reviews
exist because the human chose them. **If something is blocked, the
right answer is ALWAYS to fix the underlying cause — not to find
a way around it.**

STRICTLY FORBIDDEN, no exceptions, in every repo, under every
blocker, even if the user later asks for any of these:

- **Direct push to the default branch.** All changes go via
  PR/MR. If `git push origin main` is blocked by protection,
  that's the system working.
- **`git push --force` / `--force-with-lease` to a shared
  branch.** Rewriting history breaks downstream consumers.
- **Admin-merge / "bypass branch protections"** flags
  (`gh pr merge --admin`, GitLab's "skip merge train", etc.).
- **Disabling / deleting / making-optional any branch-protection
  rule, ruleset, environment rule, or required check.**
- **Marking a required check as "not required" / "skip
  required" / "auto-approve".** If a check fails, FIX the
  finding; never make the check optional.
- **Removing or narrowing the `on:` trigger of a required CI
  workflow** so it stops running on PRs. Example of FORBIDDEN
  behaviour in the wild (GitHub Actions):
    ```yaml
    on:
      pull_request:
        branches: [main]
      push:
        branches: [main]
    ```
    →
    ```yaml
    on:
      push:
        branches: [main]
    ```
  Same applies on GitLab: do NOT remove `merge_request_event`
  from `.gitlab-ci.yml`'s `rules.if`, do NOT add `when: never`
  on a required job to silence it. Fix the underlying failure
  or fix the matrix.
- **Resolving / dismissing / overriding review requests
  without the reviewer's explicit approval.**
- **Editing/deleting protected workflow files on `main`** to
  make a CI run "succeed" by removing the failing step.
- **Self-approving PRs/MRs with admin powers.** Even when the
  bot has admin on the repo, its own PRs need a non-bot review.
- **Force-cancelling a CI check-run / pipeline-job** to make it
  appear passing.
- **Renaming the default branch / moving the protected ref.**
- **Using `BYPASS_*` / `--admin` / `--no-verify` flags** on any
  git or provider CLI call to skip hooks/checks.

⚠️ **Inherited bypasses count.** If you check out a branch and
find that a required workflow (CodeQL, SAST, Terraform Plan,
etc.) is MISSING a `pull_request:` / `merge_request_event:`
trigger that the project's `main` branch has, you must
**RESTORE the trigger as your first commit on that branch**.
Continuing to work on a branch with an inherited bypass =
participating in the bypass.

Detection protocol: read every `.github/workflows/*.yml` and
`.gitlab-ci.yml` on your branch, compare each `on:` / `rules.if`
to the version on `main`. If your branch has a NARROWER trigger
than `main`, that's an inherited bypass. Restore the broader
trigger before your next commit.

DO NOT believe your own status comments. If you wrote
"✅ CodeQL fixed" / "✅ Pipeline green" after a config-only edit,
that comment is WRONG when the trigger is missing — the check
literally doesn't run, so no result appears. Verify via the
provider's MCP that a check_run / pipeline-job actually exists
on your PR/MR before claiming it's fixed.

### Correct workflow when blocked

1. **Read the blocker output verbatim.** What rule is blocking —
   a failing check name, a missing reviewer, a protected-ref
   message?

2. **EXHAUSTIVELY diagnose what you can do yourself FIRST.** Do
   NOT post "please do X" to the user until you've actually
   verified you can't do X. The checks to run before asking:
   - **Self-permission probe.** Can you actually do the thing?
     - For GitHub repo perms: list collaborators via the
       github MCP.
     - For GitLab: members API on the project.
     - For Azure RBAC: `mcp.servers.azure.az_role_assignments`
       on yourself + the relevant scope.
     - For k8s RBAC: `kubectl auth can-i …` (via
       `mcp.servers.k8s` where possible).
   - **Existing UI/CLI affordances.** Most "I can't do X" turns
     out to be "I haven't tried the right flag yet". Use
     `listTools` against the relevant MCP first.
   - **Documentation lookup.** Search the relevant reference
     docs through the right MCP before guessing.

3. **Fix the underlying cause** until the blocker resolves on
   its own:
   - Failing CI → fix the code/config until CI is green.
   - Missing reviewer → request the right reviewer (Copilot,
     CodeQL, or another collaborator), iterate on feedback.
   - Required signed commits → set git config to sign,
     re-commit, push.
   - Required up-to-date with base → rebase on `main`, push,
     let CI re-run.

4. **Surface progress in an issue/PR comment** with what's
   blocking, what you've already tried, and what you're doing
   next — never silently side-step.

5. **Only THEN, if you genuinely cannot fix the cause** (e.g.
   the rule requires an action only the repo owner can perform,
   the bot has no Azure write role anywhere), post a blocker
   comment to the user that explicitly lists:
   - what you tried (concrete tool calls + error outputs)
   - why those didn't work (verbatim error)
   - exactly what the user needs to change on their side
   Then yield. Do NOT try a workaround.

In short: **the green-checks-and-approvals path is the only
path**. There is no shortcut, and you must never invent one.
The user can override these rules manually in their own UI;
you cannot.

## Step 1 — pick the next issue (EXACTLY ONE)

Per-provider tool name (see TOOLS-github.md / TOOLS-gitlab.md):

- GitHub: `mcp.servers.github.search_issues
  query="assignee:@me is:issue is:open repo:<OWNER>/<REPO>"`
- GitLab: `mcp.servers.gitlab_cloud.list_issues
  assignee_id=<me> state="opened"` (similar for `gitlab_local`
  if a self-hosted instance is configured)

Pick the **single** highest-priority one (label "priority",
body urgency, or just the lowest issue number if no signal).

**Do NOT** decide "issues X and Y look related, I'll do them
together" — the mantra rules that out. If you see 5 open
issues, you work issue X1 alone through merge, then come back
for X2 in a fresh subagent spawn, and so on.

Read the issue body. Cache its `state` field.

⚠️ **Re-verify the issue is still open BEFORE every commit,
push, PR/MR open, or comment.** Issues get closed mid-flight by
human merges, automation, or the user manually. If your current
target issue is `state="closed"` when you re-check:

1. STOP — do not push the in-progress changes.
2. Close any open PR/MR for this branch with a comment
   "Closed: target issue #<n> was closed mid-flight, work
   aborted."
3. Delete the local branch + remote branch.
4. Yield — the next watchdog tick picks a still-open issue if
   one exists.

The cheap re-check: one provider-MCP `get_issue` call. Run it at
the top of Steps 4, 5, 6, AND inside any poll-loop. Don't waste
a single commit/push on a closed issue.

## Step 1.5 — read-and-react (👍 every comment you understand)

Every time you READ a comment (issue comment, PR conversation
comment, PR inline review comment, MR note, or thread reply)
that you didn't write yourself, react to it with `+1` (👍) the
moment you've understood it. The 👍 is your "I read &
understood" signal to the human — never silent.

⚠️ **NEVER 👍 your own comments.** Before calling any reaction
tool, check the comment's `user.login` / `author.username`. If
it equals your own bot login (from your provider's `whoami` /
`get_me` tool), SKIP that comment.

Provider-specific reaction tools (see provider doc for exact
names):

- GitHub: `mcp.servers.github.add_issue_comment_reaction`
  (issue / PR top-level),
  `add_pull_request_review_comment_reaction` (inline)
- GitLab: `award_emoji` on the note id (cloud or local MCP
  depending on the host)

Cadence — re-scan for new comments at minimum at these moments:

1. At session start (before any other work).
2. Before every `git commit`.
3. Between every poll of a long-running tool
   (`terraform apply`, `kubectl wait`, pipeline polling).
4. After every successful `git push`.
5. Before opening a PR/MR and during review polling.

Forbidden fallbacks:

- Do NOT post a reply comment like "Acknowledged @user's
  instruction" instead of reacting. The 👍 is the signal; the
  ack-comment is noise.
- Do NOT use `exec curl` against any host's API — `exec` strips
  tokens. The provider's MCP is the only viable path.

## Steps 2–3 — scratch clone + branch setup

```
mkdir -p ~/.openclaw/workspace/scratch/<OWNER>
if [ ! -d ~/.openclaw/workspace/scratch/<OWNER>/<REPO>/.git ]; then
  git clone <https-clone-url> ~/.openclaw/workspace/scratch/<OWNER>/<REPO>
  cd ~/.openclaw/workspace/scratch/<OWNER>/<REPO>
  git fetch origin
fi
git checkout -b feature/issue-<number>     # off the freshly-advanced default branch
```

### Step 2.5 — STALE-BRANCH CLEANUP (mandatory, run TWICE per cycle: once at session start, once after a successful merge)

The mantra "1 issue = 1 branch = 1 PR/MR" can only hold if
leftover branches from past violations are deleted. List every
remote branch authored by you that is NOT the default branch
and NOT the branch for your current target issue. Delete each:

```
# list branches via provider MCP (list_branches)
# for each branch whose tip-commit author == you (whoami):
#   - if it's `main`/`master` or `feature/issue-<current-target>`: keep
#   - else: close any open PR/MR for it with "Closed: superseded by
#           feature/issue-<target> per 1-issue-1-PR rule" comment,
#           then delete the branch (provider MCP delete_branch /
#           delete_ref ref="heads/<branch-name>")
#   - also delete the local copy: `git branch -D <name>`
```

## Step 4 — open the PR/MR and run it through review

When the work for ONE branch is feature-complete, push the final
commit, then open a PR/MR with `Closes #<number>` linking the
issue. Request reviews from the project's default reviewers
(GitHub: CodeQL auto + `request_copilot_review`; GitLab: SAST
auto + the project's approval rules).

Poll PR/MR state via the provider MCP every ~30s. Cap polling
at ~10 min. If still pending, yield with a status comment so
the next subagent spawn can resume.

## Step 5 — address review feedback autonomously

For each `CHANGES_REQUESTED` review or each CI finding:
- Read the review comments + the specific line/file targets.
- Make a focused follow-up commit on the same branch
  addressing the feedback. Push.
- Comment on the PR/MR: "Addressed <reviewer>'s feedback in
  <sha>".
- Loop back to "poll state" until checks pass.

Heuristic: if the SAME reviewer requests changes more than 2
times on the same area, stop, post a comment summarizing the
disagreement, and END.

## Step 6 — squash-merge and clean up

When required CI is GREEN and at least one approval (typically
Copilot or your project's bot reviewer) is APPROVED, merge:

- GitHub: `mcp.servers.github.merge_pull_request
  merge_method="squash"`
- GitLab: `mcp.servers.gitlab_*.accept_merge_request
  squash=true`

After merge:
- Delete remote branch (provider MCP delete_branch /
  delete_ref).
- `git checkout main && git pull origin main && git branch -D
  feature/issue-<number>`.
- Comment on the issue: "Merged via PR/MR #<n> (squash).
  Resolved."
- The issue auto-closes via `Closes #<n>` in the PR/MR body.

## Step 7 — what's next (mantra wins)

Go back to Step 1 and pick the NEXT assigned issue. There is no
"start work in parallel" — Step 2.5 deleted leftover branches,
your scratch is clean, your worktree is on `main`, your repo
has zero open PRs/MRs authored by you. Begin the next cycle.

The legacy mode of opening multiple parallel PRs to "make
progress on several fronts" is FORBIDDEN. If you find yourself
about to `git checkout -b feature/<something-different>` while
your current PR/MR is still open, STOP and go back to driving
the current PR/MR to merge.

## Subagent task template (for the watchdog when calling sessions_spawn)

```
You are a long-running coding subagent for <OWNER>/<REPO>
(hosted on <github|gitlab.com|self-hosted-gitlab>).

Invariant: you are the ONLY subagent on this repo. Do not start
parallel work elsewhere. Read TOOLS-gitflow.md for the canonical
workflow (mantra, ABSOLUTE rule, Steps 1–7). Read
TOOLS-github.md or TOOLS-gitlab.md for the host-specific MCP
names, auth env vars, and CLI you should use.

Execute.
```

The host-specific docs translate the generic step language into
concrete tool calls. The mantra and ABSOLUTE rule are the same
on every host; never re-derive them from a host-specific doc.
