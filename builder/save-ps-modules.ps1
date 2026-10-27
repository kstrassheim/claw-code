# Saves Pester and the Az family into $env:PS_MODULE_PATH, one package at a
# time, each retried on its own.
#
# WHY NOT `Save-Module -Name Az`
# Az is a meta-module over ~100 packages, and the PowerShell Gallery answers a
# small share of package downloads with a spurious 404 (the same URL returns
# 302 to the CDN on the next request). Save-Module fetches the whole family as
# one operation and gives up on the first miss, so the more packages, the
# likelier a build dies; retrying the whole save does not help either, because
# it starts over and meets a fresh 404 somewhere else. Retrying per package
# turns a ~1-in-6 miss into a non-event.
#
# Dependencies are saved at the version Az pins them to (the lower bound of
# each range — exact for every submodule, a minimum for Az.Accounts) and with
# -SkipDependencyCheck, so nothing resolves past the pinned family. The module
# assertion in the Dockerfile after this script is still what proves the save
# worked; this only makes the save stop failing for reasons unrelated to it.
$ErrorActionPreference = 'Stop'
$dest = $env:PS_MODULE_PATH
New-Item -ItemType Directory -Force -Path $dest | Out-Null

function Invoke-Retried([string]$What, [scriptblock]$Action) {
    $attempts = 8
    foreach ($i in 1..$attempts) {
        try { return & $Action }
        catch {
            if ($i -eq $attempts) { throw "$What failed after $attempts attempts: $($_.Exception.Message)" }
            Write-Host "  $What failed (attempt $i/$attempts), retrying: $($_.Exception.Message)"
            Start-Sleep -Seconds (2 * $i)
        }
    }
}

function Save-Pinned([string]$Name, [string]$Version) {
    Invoke-Retried "save $Name $Version" {
        Save-PSResource -Name $Name -Version $Version -Path $dest -Repository PSGallery `
            -TrustRepository -SkipDependencyCheck -Quiet
    }
}

Save-Pinned 'Pester' $env:PESTER_VERSION

$az = Invoke-Retried "find Az $env:AZ_VERSION" {
    Find-PSResource -Name Az -Version $env:AZ_VERSION -Repository PSGallery
}
foreach ($dep in $az.Dependencies) {
    Save-Pinned $dep.Name $dep.VersionRange.MinVersion.ToNormalizedString()
}
Save-Pinned 'Az' $env:AZ_VERSION
'saved Pester {0} and Az {1} with {2} dependencies' -f $env:PESTER_VERSION, $env:AZ_VERSION, $az.Dependencies.Count
