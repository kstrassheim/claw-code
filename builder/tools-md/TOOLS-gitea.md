<!--
  Gitea-specific bits ONLY. The provider-agnostic workflow rules
  (mantra, ABSOLUTE rule, Steps 1–7, stale-branch cleanup,
  read-and-react) live in TOOLS-gitflow.md — read that first.
  This file just translates those steps to the `tea` CLI and the
  Gitea MCP on this pod.
-->

---

# Gitea — `tea` CLI + `mcp.servers.gitea`

Gitea access is **optional**, in the sense the forge layer means it: the
deployment has it only when both `GITEA_URL` and `GITEA_API_TOKEN` are set.
If they are not, there is no Gitea host, nothing below applies, and that is
the normal state rather than a fault. Half a pair is not a host.

| Surface | Targets | Env vars |
|---|---|---|
| `mcp.servers.gitea` | `$GITEA_URL` | `GITEA_HOST=$GITEA_URL`, `GITEA_ACCESS_TOKEN=$GITEA_API_TOKEN` |
| `tea` CLI | same, once logged in | reads its own config, see below |

## The vocabulary is GitHub's, not GitLab's

A change is a **pull request**, an issue and a pull request share one
comment collection, and both are addressed by `number` within
`owner/repo`. If you have worked GitHub, the shapes will look familiar.

Three places it is NOT GitHub, and each fails quietly rather than loudly:

1. **A label is removed by numeric id**, not by name. `tea` hides this;
   the API does not.
2. **Creating an issue takes label IDs**; adding a label to an issue that
   already exists takes names.
3. **A merged pull request still reports `state: closed`.** Only the
   `merged` flag tells a merge from an abandonment.

Two things Gitea simply does not have, so do not go looking:

- **no code scanning** — there is no host-raised security-findings surface;
- **no commit comments** — a note attaches to an issue or a pull request,
  never to a commit on its own.

CI arrives as **commit statuses**, and Actions job logs live behind
`/actions/runs` → `/actions/runs/{run}/jobs` → `/actions/jobs/{id}/logs`.

## `tea` first-run

`tea` keeps its own login file rather than reading the environment:

```bash
tea login add --name pod --url "$GITEA_URL" --token "$GITEA_API_TOKEN"
tea login default pod
```

After that the usual verbs work — `tea issues`, `tea pulls`, `tea pr create`,
`tea pr merge`. Prefer the MCP for anything the MCP covers; `tea` is for the
gaps and for quick reads.

## What the planners use

Nothing here. Every question the bot asks a code host goes through
`forge.py` and its `GiteaForge` implementation — not through `tea` and not
through the MCP. These two are for interactive work inside a session; the
planners must not shell out to them, for the reason
`test_no_forge_calls_outside_forge` exists.
