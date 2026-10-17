<#
.SYNOPSIS
Read-only checks after a deploy: health, deployed SHA, restarts, new tracebacks and backup state.

.DESCRIPTION
Run from the checkout on the tower right after `docker compose up -d`, as
docs/deployment.md describes. It only reads: an HTTP GET of /health/,
`docker ps`, `docker inspect`, `docker logs`, `git rev-parse` and the backup
status file. It never restarts, recreates or writes anything.

It prints one line per check and exits 1 with a one-line reason per failure,
or 0 when every check passes. Output carries only states, counts, exception
types and SHAs; never a log line, an error message or an amount.

.EXAMPLE
$DeployStart = Get-Date
# ... build and `up -d` ...
.\ops\post-deploy-check.ps1 -ConfigPath $Config -Since $DeployStart
#>
[CmdletBinding()]
param(
    # Read for DJANGO_CSRF_TRUSTED_ORIGINS and BACKUP_DIR only, when the two
    # parameters below are not given. No other value is read or printed.
    [string]$ConfigPath = 'D:\financial-planner-config\production.env',
    [string]$ProjectName = 'financial-planner',
    # Defaults to the first DJANGO_CSRF_TRUSTED_ORIGINS entry plus /health/.
    [string]$HealthUrl,
    # Defaults to `git rev-parse origin/main`; run `git fetch origin` first.
    [string]$ExpectedSha,
    # When the deploy started. Defaults to when the app and background
    # containers were created, which `up -d` does for a new image.
    [Nullable[datetime]]$Since,
    # How long every container must stay running without a restart.
    [int]$StableSeconds = 120,
    # Defaults to BACKUP_DIR\health\status.
    [string]$StatusPath,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Services = @('db', 'app', 'backup', 'background')
# Images built from this repository carry the revision label.
$BuiltServices = @('app', 'backup', 'background')
$RevisionLabel = 'org.opencontainers.image.revision'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Helper = Join-Path $RepoRoot 'scripts\prod_signals.py'
$script:Failures = New-Object System.Collections.Generic.List[string]
$script:HealthUnreachable = $false

function Add-Failure([string]$Reason) {
    $script:Failures.Add($Reason)
    Write-Host "FAIL $Reason"
}

function Write-Pass([string]$Message) {
    Write-Host "ok   $Message"
}

function Invoke-Native([string]$File, [string[]]$Arguments) {
    # Windows PowerShell 5.1 turns a native command's stderr into errors that
    # 'Stop' would throw on, so drop stderr and judge by the exit code.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $File @Arguments 2>$null
        $code = $LASTEXITCODE
    } catch {
        $output = @()
        $code = -1
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ Output = @($output); ExitCode = $code }
}

function ConvertFrom-EnvValue([string]$Raw) {
    # Compose env-file rules: a quoted value ends at its closing quote, and an
    # unquoted one ends at a " #" comment.
    $text = $Raw.Trim()
    if ($text -match "^'([^']*)'") { return $Matches[1] }
    if ($text -match '^"((?:[^"\\]|\\.)*)"') { return $Matches[1] -replace '\\(["\\])', '$1' }
    return ($text -replace '\s+#.*$', '').Trim()
}

function Read-EnvValue([string]$Path, [string]$Key) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $value = $null
    foreach ($line in Get-Content -LiteralPath $Path) {
        # Compose lets a later line override an earlier one.
        if ($line -match "^\s*(?:export\s+)?$([regex]::Escape($Key))\s*=(.*)$") {
            $value = ConvertFrom-EnvValue $Matches[1]
        }
    }
    return $value
}

function ConvertFrom-DockerTime([string]$Value) {
    # Docker writes nanoseconds; .NET parses at most seven fractional digits.
    if ($Value -notmatch '^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d{1,7})?\d*(Z|[+-]\d\d:\d\d)$') { return $null }
    $text = $Matches[1] + $(if ($Matches[2]) { $Matches[2] } else { '' }) + $Matches[3]
    return [datetime]::Parse($text, [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AdjustToUniversal)
}

function Get-ServiceContainers([string]$Service) {
    $ids = Invoke-Native 'docker' @('ps', '-a', '-q', '--no-trunc',
        '--filter', "label=com.docker.compose.project=$ProjectName",
        '--filter', "label=com.docker.compose.service=$Service")
    if ($ids.ExitCode -ne 0) { throw 'docker ps failed; is Docker Desktop running?' }
    $found = @()
    foreach ($id in $ids.Output) {
        if (-not $id) { continue }
        $inspect = Invoke-Native 'docker' @('inspect', $id)
        if ($inspect.ExitCode -ne 0) { continue }
        $info = @(($inspect.Output -join "`n") | ConvertFrom-Json)[0]
        $revision = $null
        if ($info.Config.Labels -and $info.Config.Labels.PSObject.Properties[$RevisionLabel]) {
            $revision = $info.Config.Labels.$RevisionLabel
        }
        $found += [pscustomobject]@{
            Service      = $Service
            Id           = $id
            Name         = $info.Name.TrimStart('/')
            Status       = $info.State.Status
            RestartCount = [int]$info.RestartCount
            StartedAt    = $info.State.StartedAt
            Started      = ConvertFrom-DockerTime $info.State.StartedAt
            Created      = ConvertFrom-DockerTime $info.Created
            Revision     = $revision
        }
    }
    return , $found
}

function Get-Snapshot {
    $snapshot = @{}
    foreach ($service in $Services) { $snapshot[$service] = Get-ServiceContainers $service }
    return $snapshot
}

function Test-Health {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $request = [Net.HttpWebRequest]::Create($HealthUrl)
    $request.Method = 'GET'
    $request.Timeout = 10000
    $request.AllowAutoRedirect = $false
    # Tailscale Serve sets this; on a loopback URL it stops the HTTPS redirect.
    $request.Headers.Add('X-Forwarded-Proto', 'https')
    $response = $null
    try {
        $response = $request.GetResponse()
    } catch [Net.WebException] {
        $response = $_.Exception.Response
        if ($null -eq $response) {
            Add-Failure "health: $HealthUrl unreachable ($($_.Exception.Status))"
            $script:HealthUnreachable = $true
            return $null
        }
    }
    try {
        $code = [int]$response.StatusCode
        $revision = $response.Headers['X-App-Revision']
    } finally {
        $response.Close()
    }
    if ($code -eq 200) { Write-Pass "health: 200" } else { Add-Failure "health: returned $code, expected 200" }
    return $revision
}

function Test-Revision([string]$HealthRevision, $Snapshot) {
    if (-not $ExpectedSha) {
        $rev = Invoke-Native 'git' @('-C', $RepoRoot, 'rev-parse', '--verify', 'origin/main')
        if ($rev.ExitCode -ne 0 -or -not $rev.Output) {
            Add-Failure 'revision: cannot read origin/main; run git fetch origin or pass -ExpectedSha'
            return
        }
        $script:ExpectedSha = [string]$rev.Output[0]
    }
    $expected = $ExpectedSha.Trim().ToLowerInvariant()
    $short = $expected.Substring(0, [Math]::Min(12, $expected.Length))
    if ($script:HealthUnreachable) {
        # Already reported; the image labels below still show what is deployed.
    } elseif (-not $HealthRevision) {
        Add-Failure 'revision: /health/ sent no X-App-Revision header'
    } elseif ($HealthRevision -eq 'unknown') {
        Add-Failure 'revision: the app image has no GIT_SHA; rebuild with $env:GIT_SHA set'
    } elseif (-not $expected.StartsWith($HealthRevision.ToLowerInvariant())) {
        Add-Failure "revision: /health/ serves $HealthRevision, expected $short"
    } else {
        Write-Pass "revision: /health/ serves $HealthRevision"
    }
    foreach ($service in $BuiltServices) {
        foreach ($container in $Snapshot[$service]) {
            $label = if ($container.Revision) { $container.Revision.ToLowerInvariant() } else { 'none' }
            if ($label -ne $expected) {
                $shown = $label.Substring(0, [Math]::Min(12, $label.Length))
                Add-Failure "revision: $service runs an image built from $shown, expected $short; recreate it"
            }
        }
    }
}

function Test-Containers($Before, $After, [datetime]$DeployStart) {
    # Docker's times are UTC, and comparing datetimes ignores their kind.
    $deployUtc = $DeployStart.ToUniversalTime()
    foreach ($service in $Services) {
        $first = @($Before[$service])
        $last = @($After[$service])
        if ($last.Count -eq 0) {
            Add-Failure "containers: $service has no container in project $ProjectName"
            continue
        }
        $healthy = $true
        foreach ($now in $last) {
            $then = @($first | Where-Object { $_.Id -eq $now.Id })
            if ($now.Status -ne 'running') {
                Add-Failure "containers: $service is $($now.Status), expected running"
                $healthy = $false
            } elseif ($then.Count -eq 0) {
                Add-Failure "containers: $service was replaced during the ${StableSeconds}s window"
                $healthy = $false
            } elseif ($now.RestartCount -ne $then[0].RestartCount -or $now.StartedAt -ne $then[0].StartedAt) {
                Add-Failure "containers: $service restarted during the ${StableSeconds}s window (RestartCount $($now.RestartCount))"
                $healthy = $false
            } elseif ($now.Created -and $now.Created -ge $deployUtc -and $now.RestartCount -gt 0) {
                Add-Failure "containers: $service restarted $($now.RestartCount) time(s) since the deploy"
                $healthy = $false
            } elseif ($now.Created -and $now.Created -lt $deployUtc -and $now.Started -and $now.Started -ge $deployUtc) {
                # A container the deploy kept (usually db) should not have started again since.
                Add-Failure "containers: $service was kept by the deploy but started again since it began"
                $healthy = $false
            }
        }
        if ($healthy) { Write-Pass "containers: $service running, RestartCount $(($last | ForEach-Object { $_.RestartCount }) -join ',')" }
    }
}

function Invoke-Helper([string[]]$Arguments) {
    $result = Invoke-Native $Python (@($Helper) + $Arguments)
    if ($result.ExitCode -ne 0 -or -not $result.Output) { return $null }
    return ($result.Output -join "`n") | ConvertFrom-Json
}

function Test-Tracebacks($Snapshot, [datetime]$DeployStart) {
    $sinceText = $DeployStart.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", [Globalization.CultureInfo]::InvariantCulture)
    foreach ($service in $Services) {
        foreach ($container in $Snapshot[$service]) {
            $counts = Invoke-Helper @('tracebacks', '--container', $container.Id, '--since', $sinceText)
            if ($null -eq $counts) {
                Add-Failure "tracebacks: cannot count $service logs (is $Python on PATH?)"
                continue
            }
            if ($counts.tracebacks -eq 0) {
                Write-Pass "tracebacks: $service has none since $sinceText"
                continue
            }
            $types = ($counts.types.PSObject.Properties | ForEach-Object { "$($_.Name) x$($_.Value)" }) -join ', '
            Add-Failure "tracebacks: $service logged $($counts.tracebacks) since $sinceText ($types)"
        }
    }
}

function Test-Backup {
    if (-not (Test-Path -LiteralPath $StatusPath -PathType Leaf)) {
        Add-Failure 'backup: no status file; run a backup before relying on this deploy'
        return
    }
    $status = Invoke-Helper @('backup-status', $StatusPath)
    if ($null -eq $status) {
        Add-Failure "backup: cannot read the status file (is $Python on PATH?)"
    } elseif ($status.state -eq 'error') {
        Add-Failure "backup: status is error ($(@($status.errors) -join ', '))"
    } elseif ($status.state -eq 'stale') {
        # Stale is the nightly alert's business, not a reason to roll back a deploy.
        Write-Host "warn backup: status is stale ($(@($status.stale) -join ', ')); last success $($status.last_success_at)"
    } else {
        Write-Pass "backup: status ok, last success $($status.last_success_at)"
    }
}

try {
    if (-not $HealthUrl) {
        $origins = Read-EnvValue $ConfigPath 'DJANGO_CSRF_TRUSTED_ORIGINS'
        if (-not $origins) { throw "pass -HealthUrl or a -ConfigPath with DJANGO_CSRF_TRUSTED_ORIGINS" }
        $HealthUrl = ($origins.Split(',')[0].Trim().TrimEnd('/')) + '/health/'
    }
    if (-not $StatusPath) {
        $backupDir = Read-EnvValue $ConfigPath 'BACKUP_DIR'
        if (-not $backupDir) { throw "pass -StatusPath or a -ConfigPath with BACKUP_DIR" }
        $StatusPath = Join-Path $backupDir 'health\status'
    }

    $before = Get-Snapshot
    if (-not $Since) {
        $created = @('app', 'background' | ForEach-Object { $before[$_] } | ForEach-Object { $_.Created } | Where-Object { $_ })
        if ($created.Count -eq 0) { throw 'no app or background container to date the deploy; pass -Since' }
        $Since = ($created | Sort-Object | Select-Object -First 1).ToLocalTime()
    }
    $deployStart = [datetime]$Since

    $revision = Test-Health
    Test-Revision $revision $before

    if ($StableSeconds -gt 0) {
        Write-Host "wait $StableSeconds s for restarts"
        Start-Sleep -Seconds $StableSeconds
    }
    $after = Get-Snapshot
    Test-Containers $before $after $deployStart
    Test-Tracebacks $after $deployStart
    Test-Backup
} catch {
    # Messages raised above are this script's own; anything else is reported by type only.
    $message = if ($_.Exception -is [Management.Automation.RuntimeException] -and $_.Exception.WasThrownFromThrowStatement) {
        $_.Exception.Message
    } else {
        $_.Exception.GetType().Name
    }
    Add-Failure "check: $message"
}

if ($script:Failures.Count -gt 0) {
    Write-Host "post-deploy check FAILED: $($script:Failures.Count) problem(s). Roll back per docs/deployment.md."
    exit 1
}
Write-Host 'post-deploy check passed'
exit 0
