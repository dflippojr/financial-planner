<#
.SYNOPSIS
  Registers a pinned GitHub Actions runner as a hidden per-user logon task.

.DESCRIPTION
  The registration token is used once and is not saved. Default values install
  the deploy tower runner (`agent-harness-tower`; `deploy-tower` in ci-cd.yml),
  not SonarCloud or review. Pass -Labels agent-harness-ci and a distinct
  -InstallDir / -Name / -TaskName for each pytest pool member (three members:
  github-runner-ci, -ci-2, -ci-3) so those jobs do not share a queue with
  deploy. Pass -Labels agent-harness-review and distinct -InstallDir / -Name /
  -TaskName for each review pool member.

.EXAMPLE
  $token = gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
  .\ops\github\install-runner.ps1 -Token $token
  .\ops\github\install-runner.ps1 -Token $token -InstallDir D:\Agents\github-runner-ci -Name dflippotower-agent-harness-ci -TaskName AgentHarness-GitHubRunner-CI -Labels agent-harness-ci
  .\ops\github\install-runner.ps1 -Token $token -InstallDir D:\Agents\github-runner-ci-2 -Name dflippotower-agent-harness-ci-2 -TaskName AgentHarness-GitHubRunner-CI-2 -Labels agent-harness-ci
  .\ops\github\install-runner.ps1 -Token $token -InstallDir D:\Agents\github-runner-ci-3 -Name dflippotower-agent-harness-ci-3 -TaskName AgentHarness-GitHubRunner-CI-3 -Labels agent-harness-ci
#>
param(
    [Parameter(Mandatory)][string]$Token,
    [string]$Repo = 'dflippojr/agent-harness',
    [string]$InstallDir = 'D:\Agents\github-runner',
    [string]$WorkDir = '_work',
    [string]$Name = 'dflippotower-agent-harness',
    [string]$TaskName = 'AgentHarness-GitHubRunner',
    [string]$Labels = 'agent-harness-tower',
    [string]$Version = '2.337.0',
    [string]$Sha256 = '1150692afa94e71f872017e254ea55b6eece1eece3fe7e3a6d4c93d0a1b85cfc'
)
$ErrorActionPreference = 'Stop'
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

Push-Location $InstallDir
try {
    & .\config.cmd --unattended --url "https://github.com/$Repo" --token $Token --name $Name `
        --labels $Labels --work $WorkDir
    if ($LASTEXITCODE -ne 0) { throw "runner registration failed with exit code $LASTEXITCODE" }
} finally { Pop-Location }

$launchArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& '$InstallDir\run.cmd'`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $launchArgs -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "GitHub Actions runner $Name ($Labels) for $Repo." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
