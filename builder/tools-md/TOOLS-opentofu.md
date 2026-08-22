<!--
  Describes the OpenTofu CLI + the OpenTofu registry MCP wired into
  this image, and when to reach for it instead of terraform.
-->

---

# OpenTofu — the other IaC binary

You have:
- **`tofu`** CLI (pinned via `OPENTOFU_VERSION` in the repo's
  `VERSIONS` file). The subcommands are the ones you already know:
  `init`, `plan`, `apply`, `destroy`, `validate`, `fmt`, `show`,
  `state list / show`, `output`.
- **`mcp.servers.opentofu`** — the OpenTofu registry MCP, for
  looking up providers, modules, resources and data sources without
  scraping registry HTML. Check the live `listTools` for the current
  surface.

## Which binary to use

**Read the repository, do not choose.** `tofu` and `terraform` are
forks of the same tool and they are NOT interchangeable on a project
that already exists:

- the state file records which binary wrote it, and running the other
  one against it is a migration, not a command;
- `.terraform.lock.hcl` pins provider hashes per binary, and the two
  registries serve different ones;
- CI for the project runs one of them, so a plan you produce with the
  other one is not the plan that will be applied.

So the rule is mechanical:

| What the repository has | Use |
|---|---|
| `.opentofu/` or a workflow calling `tofu` | `tofu` |
| `.terraform/`, or a workflow calling `terraform` | `terraform` |
| a lockfile written by one of them | that one |
| genuinely nothing yet | ask, or follow the project's README |

If the two signals disagree, say so in your status comment rather
than picking one — a repository midway through a migration is a
question for a person, and guessing wrong rewrites state.

## Capability boundaries

Identical to terraform's, and for the same reason: your reach is
**exactly what the underlying credentials grant**. There is no
MCP-layer enforcement — the providers' IAM is the only guard. See
TOOLS-terraform.md for the provider/credential table, which applies
here unchanged.

`apply` and `destroy` against real infrastructure are governed by
the same rules as every other destructive action: if the issue does
not plainly ask for it, ask first.
