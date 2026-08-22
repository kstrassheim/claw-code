<!--
  Azure DevOps-specific bits ONLY. The provider-agnostic workflow rules
  (mantra, ABSOLUTE rule, Steps 1–7, stale-branch cleanup,
  read-and-react) live in TOOLS-gitflow.md — read that first.
  This file translates those steps to `az devops` and the Azure
  DevOps MCP on this pod.
-->

---

# Azure DevOps — `az devops` + `mcp.servers.azuredevops`

**Optional**, the way every host here is: the deployment has it only when
`AZDO_ORG` and `AZDO_API_TOKEN` are both set. If they are not, there is no
Azure DevOps host and nothing below applies.

| Surface | Targets | Auth |
|---|---|---|
| `mcp.servers.azuredevops` | `$AZDO_ORG` | **Azure CLI** — needs `az login` |
| `az devops` | same | `AZURE_DEVOPS_EXT_PAT=$AZDO_API_TOKEN` |

**The MCP does NOT use the PAT, and `az login` is not the PAT.** Two different
credentials:

- the **PAT** (`$AZDO_API_TOKEN`) is an Azure DevOps credential. The forge
  layer and `az devops` both use it, and neither needs a login step.
- **`az login`** signs in an *Entra identity* (`$ENTRA_USERNAME`). The MCP
  uses that, via `--authentication azcli`, and nothing else does.

**Nothing on this pod runs `az login` for you.** Driving it is your job — see
TOOLS-entra.md, `az login --use-device-code --tenant "$ENTRA_TENANT_ID"` with
`entra-totp` for MFA. Until you have, the MCP answers nothing, and that
failure looks like a broken server rather than a missing login. Check
`az account show` first.

That Entra identity also has to be a MEMBER of the Azure DevOps organization.
Being signed in to the tenant grants nothing there on its own.

It is also **public preview**. Treat a tool that misbehaves as the preview
misbehaving, not as the host being unreachable, and fall back to `az devops`
or to asking the forge.

Neither of them is required for the bot to work an Azure DevOps repository:
the forge layer speaks HTTP with the PAT and is unaffected by both.

## This host is NOT GitHub-shaped. Read this before anything else.

**There are no issues. There are WORK ITEMS**, in a different service from
Git, typed Bug / Task / User Story. And the difference that catches everyone:

> **A work item belongs to a PROJECT, not to a repository.**

A project can hold many repositories. So "which repo is this work item
about?" has no automatic answer, and the bot resolves it by the work item's
own Git links (a branch, commit or pull request it is attached to), falling
back to the project's only repository when there is exactly one. A work item
with no link, in a project with several repositories, is **skipped** — not
guessed at.

**If you want the bot to pick up a work item, attach it to a branch or a pull
request.** That is the whole mechanism.

| Elsewhere | Here |
|---|---|
| issue | work item |
| issue body | `System.Description` |
| labels | `System.Tags` — ONE semicolon-joined string, not a list |
| closing an issue | `System.State`, whose legal values depend on the process template |
| pull request | pull request, but merging is called **completing**, and closing without merging is **abandoning** |
| review | a **vote**: 10 approved, 5 approved with suggestions, 0 none, -5 waiting for author, -10 rejected |
| CI | commit **statuses** and Builds — not check runs |

Addressing: `project/repo` in everything the bot says, the organisation being
configuration. Under the covers every Git call goes by the repository's
**GUID**, because a repository name is unique only inside its project.

Three things that simply do not exist here — do not go looking:

- **no reactions** on comments (so a comment cannot be silently acknowledged);
- **no commit comments** (a note attaches to a PR thread or a work item);
- **no code-scanning API** on the Git service (Advanced Security is a separate
  paid product with its own surface).

## `az devops` first-run

```bash
export AZURE_DEVOPS_EXT_PAT="$AZDO_API_TOKEN"
az devops configure --defaults organization="https://dev.azure.com/$AZDO_ORG"
az repos list --output table
az boards work-item show --id 42
```

The PAT goes in `AZURE_DEVOPS_EXT_PAT`; `az devops login` reads the same
value from stdin. Note the PAT **expires** — a year at most, and org policy
may force less — so a 401 here is as likely to be an expired token as a wrong
one.

## What the planners use

Nothing here. Every question the bot asks a code host goes through `forge.py`
and its `AzureDevOpsForge` implementation — not through `az devops` and not
through the MCP. These two are for interactive work inside a session; the
planners must not shell out to them, for the reason
`test_no_forge_calls_outside_forge` exists.
