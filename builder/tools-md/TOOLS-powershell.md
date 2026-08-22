<!--
  PowerShell runbook authoring + Pester unit tests, for Azure Automation
  projects. Used by the ISSUE SOLVER. The Azure-side tools that deploy and
  run these live in the TOOLS-azure-data section; PSScriptAnalyzer's
  security rules are in the TOOLS-security section.
-->

---

# PowerShell runbooks + Pester — Automation projects

An Automation project keeps its runbooks in `runbooks/` and publishes them to
an Azure Automation account with Terraform (`automation_runbooks.tf`). Adding
a runbook therefore means **three** things, not one: the script, its tests,
and its Terraform registration. A runbook that exists only as a file is not
deployed and will never run.

## What is installed

- `pwsh` (PowerShell 7) on `$PATH`
- **Pester** — the PowerShell test framework
- **`Az.Accounts`, `Az.Automation`, `Az.DataFactory`, `Az.Synapse`** — only
  these four, not the full `Az` meta-module. If you need another `Az.*`
  module, say so rather than assuming it is there.
- **PSScriptAnalyzer** — lint + security rules (TOOLS-security section)

## Writing a runbook

- One job per runbook; take inputs as a `param(...)` block with types and
  sensible defaults, so the runbook can be started with parameters from the
  Automation API.
- Authenticate with the Automation account's own managed identity
  (`Connect-AzAccount -Identity`) — a runbook must never carry a credential.
- `Set-StrictMode -Version Latest` and `$ErrorActionPreference = 'Stop'` at
  the top, so a failure fails the job instead of quietly continuing.
- Write progress with `Write-Output` / `Write-Verbose`. The tester reads the
  job output stream and treats error text there as a failure **even when the
  job status is `Completed`** — so do not print scary text on a success path,
  and do not swallow real errors into a friendly message.
- Keep the side-effecting call and the decision that leads to it in separate
  functions; that is what makes the thing testable at all.

## Writing the Pester tests

Every runbook gets a `*.Tests.ps1` beside it. These are **unit** tests: they
must run with no Azure connection and no network.

- `Mock` the `Az.*` cmdlets — assert with `Should -Invoke` that the runbook
  called what it should, with the parameters it should.
- Cover the failure paths, not just the happy one: a mocked cmdlet that
  throws, an empty result set, a missing parameter.
- Assert on behaviour, not on log text. A test that greps `Write-Output`
  strings breaks the moment someone rewords a message.
- Never let a test touch a real subscription. If a test only passes when
  Azure is reachable, it is not a unit test and belongs in the tester.

Run them before opening the MR:

```
pwsh -NoProfile -Command "Invoke-Pester ./runbooks -CI"
```

`-CI` gives a non-zero exit on failure, which is what the pipeline keys on.
**All tests must pass locally before you push.** "The pipeline will tell me"
is not the workflow here.

## Registering it

Add the runbook to `automation_runbooks.tf` following the pattern already in
that file — name, type (`PowerShell`), publish state, and the content link or
inline content. Copy the shape of an existing entry rather than inventing a
new one; the modules and naming conventions are project-specific.

## Definition of done

1. `runbooks/<Name>.ps1` exists and does one thing.
2. `runbooks/<Name>.Tests.ps1` covers success and failure paths, all passing.
3. PSScriptAnalyzer is clean (or every remaining finding is explained).
4. The runbook is registered in Terraform so it actually deploys.
5. The MR says what the runbook does and how it was tested.
