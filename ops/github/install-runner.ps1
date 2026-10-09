<#
.SYNOPSIS
  Registers a pinned, ephemeral Financial Planner review runner.

.DESCRIPTION
  The registration token is used once and is not saved. Run this script as
  a dedicated, non-administrator Windows account. The defaults install this
  repository's review runner. The
  hook is installed outside the runner directory and the checkout. Registration
  accepts one job; the owner preserves diagnostics, cleans the work directory,
  and provisions a fresh registration before the next review. See
  docs/review-runner.md. This script refuses an already configured runner.

.EXAMPLE
  $token = gh api -X POST repos/dflippojr/financial-planner/actions/runners/registration-token --jq .token
  .\ops\github\install-runner.ps1 -Token $token
#>
param(
    [Parameter(Mandatory)][string]$Token,
    [ValidateSet('dflippojr/financial-planner')][string]$Repo = 'dflippojr/financial-planner',
    [string]$InstallDir = 'D:\Agents\financial-planner-review',
    [string]$HookDir = 'D:\Agents\financial-planner-review-hooks',
    [string]$WorkDir = '_work',
    [string]$Name = 'financial-planner-review',
    [string]$TaskName = 'FinancialPlanner-GitHubRunner-Review',
    [ValidateSet('financial-planner-review')][string]$Labels = 'financial-planner-review',
    [string]$Version = '2.337.0',
    [string]$Sha256 = '1150692afa94e71f872017e254ea55b6eece1eece3fe7e3a6d4c93d0a1b85cfc'
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'run this installer as the dedicated non-administrator review account'
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$HookDir = [IO.Path]::GetFullPath($HookDir).TrimEnd('\')
$checkout = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
foreach ($excluded in @($InstallDir, $checkout)) {
    if ($HookDir -ieq $excluded -or $HookDir.StartsWith("$excluded\", [StringComparison]::OrdinalIgnoreCase)) {
        throw 'HookDir must be outside the runner application directory and checkout'
    }
}
# The scheduled task uses a PowerShell single-quoted path literal.
if ($InstallDir.Contains("'")) { throw 'InstallDir cannot contain a single quote' }
$zip = Join-Path $env:TEMP "actions-runner-win-x64-$Version.zip"
$url = "https://github.com/actions/runner/releases/download/v$Version/actions-runner-win-x64-$Version.zip"

if (Test-Path -LiteralPath (Join-Path $InstallDir '.runner')) {
    throw "a runner is already configured in $InstallDir"
}
New-Item -ItemType Directory -Force $InstallDir | Out-Null
if (-not (Test-Path -LiteralPath $zip)) { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip }
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
if ($actual -ne $Sha256) { throw "runner archive hash mismatch: $actual" }
Expand-Archive -LiteralPath $zip -DestinationPath $InstallDir -Force

New-Item -ItemType Directory -Force $HookDir | Out-Null
$hook = Join-Path $HookDir 'review-job-started.ps1'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'review-job-started.ps1') -Destination $hook -Force
$runnerEnv = Join-Path $InstallDir '.env'
$envLines = @()
if (Test-Path -LiteralPath $runnerEnv) {
    $envLines = @(Get-Content -LiteralPath $runnerEnv | Where-Object { $_ -notmatch '^ACTIONS_RUNNER_HOOK_JOB_STARTED=' })
}
# UTF-8 without a BOM: the runner reads .env as key=value, not PowerShell.
[IO.File]::WriteAllLines($runnerEnv, [string[]]($envLines + "ACTIONS_RUNNER_HOOK_JOB_STARTED=$hook"),
    (New-Object Text.UTF8Encoding($false)))

Push-Location $InstallDir
try {
    & .\config.cmd --unattended --url "https://github.com/$Repo" --token $Token --name $Name `
        --labels $Labels --work $WorkDir --ephemeral
    if ($LASTEXITCODE -ne 0) { throw "runner registration failed with exit code $LASTEXITCODE" }
} finally { Pop-Location }

$launchArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& '$InstallDir\run.cmd'`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $launchArgs -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $taskPrincipal -Description "Ephemeral GitHub Actions review runner $Name for $Repo." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
