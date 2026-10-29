<#
.SYNOPSIS
Migrate the newest verified nightly dump in a throwaway stack before a deploy that adds migrations.

.DESCRIPTION
Run from the checkout on the tower with PowerShell 7 (pwsh) before every
deploy whose release adds a migration, as docs/deployment.md describes. It:

1. starts a Docker daemon of its own: rootless dockerd in a pinned
   docker:dind-rootless container whose only host directory is the run's work
   directory. Every compose command for the candidate goes to that daemon's
   socket, never to the host's daemon, so whatever the candidate's compose
   file holds cannot reach production's containers, volumes or files;
2. builds the candidate revision's images there under a throwaway Compose
   project (fp-rehearsal-*) with its own volumes, port, dummy secrets and a
   temporary backup directory;
3. restores a copy of the newest signed nightly dump (or -DumpPath) into that
   project's empty database with the candidate's restore.sh;
4. starts the candidate's app, which runs `migrate --noinput` as production
   does, and compares every table's row count before and after;
5. starts the rest of the stack, takes one backup of the migrated database,
   then runs `reconcile_balances --json` and ops/post-deploy-check.ps1 against it;
6. always tears the throwaway project down, then removes the isolated daemon
   with its data, so no container, volume or image of the run remains.

It prints PASS or FAIL with table names, counts and states only, never a row,
a log line or a secret, and exits 0 only on PASS. It never starts the host's
Docker; when that is not reachable it fails.

.EXAMPLE
git fetch origin
./ops/deploy-rehearsal.ps1 -Candidate origin/main -ConfigPath $Config

.EXAMPLE
# Synthetic only: a dump built by scripts/build-synthetic-dump.sh.
./ops/deploy-rehearsal.ps1 -Candidate HEAD -DumpPath .tmp/synthetic.dump `
    -ExpectCountChange finance_alert, finance_aiconversation, finance_aiconversationmessage
#>
[CmdletBinding()]
param(
    # A git revision in this checkout: a SHA, a tag or a branch such as origin/main.
    [string]$Candidate,
    # The production env file. Only BACKUP_DIR is read from it, to find the newest
    # signed dump in BACKUP_DIR/nightly; no other value reaches the throwaway stack.
    [string]$ConfigPath,
    # A synthetic dump to restore instead, restored without a signed manifest.
    [string]$DumpPath,
    # Defaults to fp-rehearsal-<random>. It must carry the fp-rehearsal- prefix.
    [string]$ProjectName,
    # Loopback port for the throwaway app; 0 picks a free one.
    [int]$AppPort = 0,
    # Tables whose row count the release's migrations change on purpose, as its
    # release notes or tests/test_migrate_populated.py declare. Any other change fails.
    [string[]]$ExpectCountChange = @(),
    # Mismatched reconciliation windows production already shows (Settings → Data).
    # More than this fails.
    [int]$KnownMismatchWindows = 0,
    # How long the migrated app has to report healthy.
    [int]$MigrateTimeoutSeconds = 900,
    # Passed to the post-deploy check: how long every container must stay up.
    [int]$StableSeconds = 60,
    [string]$Python = $(if ($IsWindows) { 'python' } else { 'python3' })
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProductionProject = 'financial-planner'
$ProjectPrefix = 'fp-rehearsal-'
# Tables Django's own bookkeeping adds to whenever a release adds a migration or a model.
$GrowOnlyTables = @('django_migrations', 'django_content_type', 'auth_permission')
# What the documented deploy builds; config-check runs background's image.
$BuiltServices = @('app', 'backup', 'background')
# Compose lets the environment override --env-file, so an exported production
# setting (POSTGRES_VOLUME_NAME, BACKUP_DIR, COMPOSE_FILE...) could point the
# throwaway project at real storage. Docker gets only these.
$DockerEnvNames = @('PATH', 'HOME', 'TMPDIR', 'XDG_RUNTIME_DIR', 'SystemRoot', 'ProgramData', 'USERPROFILE',
    'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP')
$DockerEnvPrefixes = @('DOCKER_', 'BUILDKIT_')
# The isolated daemon: rootless dockerd as the image's uid 1000, pinned by
# digest. Not --privileged: it needs the host's rootlesskit AppArmor profile
# (shipped with Ubuntu) to create its user namespace, /dev/net/tun for
# slirp4netns, and seccomp and /proc unmasked so it can mount /proc for its
# containers. Host networking lets the app's 127.0.0.1 port reach the host for
# the post-deploy check; the containers themselves sit behind slirp4netns with
# the host's loopback disabled.
$IsolatedImage = 'docker:29-dind-rootless@sha256:ae083cc7de51e7a8e5a597f8fb774ca7914c408a95fc0f1477826a913479518a'
$IsolatedStartSeconds = 120
# The daemon's socket, inside the isolated container's own filesystem. The
# docker and compose clients for the candidate run there too (docker exec), so
# no host process connects to a path a candidate could replace, and every file
# compose reads (build contexts, Dockerfiles) resolves in a filesystem that
# holds nothing of the host's but the work directory.
$IsolatedSocket = 'unix:///home/rootless/docker.sock'
$RepoRoot = Split-Path -Parent $PSScriptRoot
# `pwsh -File` passes a comma list as one string.
$ExpectCountChange = @($ExpectCountChange | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$script:Failures = New-Object System.Collections.Generic.List[string]

function Add-Failure([string]$Reason) {
    $script:Failures.Add($Reason)
    Write-Host "FAIL $Reason"
}

function Write-Pass([string]$Message) {
    Write-Host "ok   $Message"
}

# Why $Name may not be torn down with `down --volumes`, or $null when it may.
function Test-RehearsalProjectName([string]$Name) {
    if (-not $Name) { return 'the project name is empty' }
    if ($Name -eq $ProductionProject) { return "refusing the production project $ProductionProject" }
    if (-not $Name.StartsWith($ProjectPrefix)) { return "project $Name lacks the $ProjectPrefix prefix" }
    # Compose's own rule for project names, so the name compose uses is this one.
    if ($Name -cnotmatch '^fp-rehearsal-[a-z0-9][a-z0-9_-]*$') { return "project $Name is not a valid lowercase compose project name" }
    return $null
}

function Assert-RehearsalProjectName([string]$Name) {
    $reason = Test-RehearsalProjectName $Name
    if ($reason) { throw $reason }
}

# row-counts.sh prints "table<TAB>count" lines.
function ConvertFrom-RowCounts([string[]]$Lines) {
    $counts = [ordered]@{}
    foreach ($line in $Lines) {
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([A-Za-z0-9_]+)\t(\d+)\s*$') { throw 'row counts: unexpected output from row-counts.sh' }
        $counts[$Matches[1]] = [long]$Matches[2]
    }
    return $counts
}

# One row per table in either snapshot: same, grew (bookkeeping), new, expected,
# changed or dropped. Changed and dropped fail unless the table is expected.
function Compare-RowCounts($Before, $After, [string[]]$Expected = @()) {
    $tables = @(@($Before.Keys) + @($After.Keys) | Sort-Object -Unique)
    foreach ($table in $tables) {
        $had = $Before.Contains($table)
        $has = $After.Contains($table)
        $old = if ($had) { $Before[$table] } else { $null }
        $new = if ($has) { $After[$table] } else { $null }
        $state = if (-not $had) {
            'new'
        } elseif (-not $has) {
            if ($Expected -contains $table) { 'expected' } else { 'dropped' }
        } elseif ($old -eq $new) {
            'same'
        } elseif ($GrowOnlyTables -contains $table -and $new -gt $old) {
            'grew'
        } elseif ($Expected -contains $table) {
            'expected'
        } else {
            'changed'
        }
        [pscustomobject]@{
            Table  = $table
            Before = $old
            After  = $new
            State  = $state
            Failed = $state -in @('changed', 'dropped')
        }
    }
}

function Format-CountRow($Row) {
    $show = { param($n) if ($null -eq $n) { '-' } else { [string]$n } }
    $prefix = if ($Row.Failed) { 'FAIL' } else { 'ok  ' }
    return '{0} counts: {1,-48} {2,9} -> {3,-9} {4}' -f $prefix, $Row.Table, (& $show $Row.Before), (& $show $Row.After), $Row.State
}

function Format-Verdict([int]$Problems, [string]$Sha, [int]$Tables, [int]$Migrations) {
    $short = if ($Sha) { $Sha.Substring(0, [Math]::Min(12, $Sha.Length)) } else { 'unresolved candidate' }
    if ($Problems -gt 0) { return "FAIL deploy rehearsal of ${short}: $Problems problem(s); do not deploy" }
    return "PASS deploy rehearsal of ${short}: $Tables tables compared, $Migrations migration(s) applied"
}

function Get-Prop($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
    return $null
}

# A new, empty directory only this user can enter, named at random rather than
# after -ProjectName. CreateTempSubdirectory creates it exclusively (mkdtemp,
# mode 0700, on Linux), so a path someone else created first, or a symlink
# they planted there, is never reused.
function New-RehearsalWorkDir {
    return [IO.Directory]::CreateTempSubdirectory('fp-rehearsal-work-').FullName
}

# What identifies the directory at $Path itself, not where a link there points:
# device and inode on Linux. Someone who can write its parent could rename it
# and put a link to real backups in its place; teardown compares this with the
# value pinned at creation before it empties or removes anything. On Windows,
# %TEMP% sits in the user's own profile, which no other account may write, so
# only a reparse point is refused there. The check can still lose a race with a
# swap between it and Docker opening the bind source: a work directory under a
# parent another account can write, without the sticky bit, is outside what
# this script defends.
function Get-DirectoryIdentity([string]$Path) {
    $item = [IO.DirectoryInfo]::new($Path)
    if (-not $item.Exists -or $null -ne $item.LinkTarget) { return $null }
    if ($IsWindows) { return $item.FullName }
    $identity = & stat -c '%d:%i' -- $Path 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $identity) { return $null }
    return ([string]@($identity)[0]).Trim()
}

# Whether $script:WorkDir is still the directory this run created.
function Test-WorkDirIdentity {
    if (-not $script:WorkDir -or -not $script:WorkDirIdentity) { return $false }
    return (Get-DirectoryIdentity $script:WorkDir) -ceq $script:WorkDirIdentity
}

# $Path with every symlink or junction along it followed, as Docker follows them
# in a bind source, or $null when it cannot be resolved. Parts that do not exist
# yet are kept as written: compose creates them as plain directories.
function Resolve-CanonicalPath([string]$Path) {
    if (-not $Path -or -not [IO.Path]::IsPathFullyQualified($Path)) { return $null }
    $separators = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $pending = [Collections.Generic.Stack[string]]::new()
    $push = {
        param([string]$Text)
        $parts = $Text.Split($separators, [StringSplitOptions]::RemoveEmptyEntries)
        for ($i = $parts.Count - 1; $i -ge 0; $i--) { $pending.Push($parts[$i]) }
    }
    $current = [IO.Path]::GetPathRoot($Path)
    & $push $Path.Substring($current.Length)
    $links = 0
    $missing = $false
    try {
        while ($pending.Count -gt 0) {
            $part = $pending.Pop()
            if ($part -eq '.') { continue }
            if ($part -eq '..') {
                if ($missing) { return $null }
                $parent = [IO.Path]::GetDirectoryName($current)
                if ($parent) { $current = $parent }
                continue
            }
            $next = [IO.Path]::Combine($current, $part)
            if (-not $missing) {
                $target = [IO.FileInfo]::new($next).LinkTarget
                if ($null -ne $target) {
                    if (++$links -gt 40) { return $null }
                    if ([IO.Path]::IsPathRooted($target)) {
                        $current = [IO.Path]::GetPathRoot($target)
                        if (-not $current) { return $null }
                        $target = $target.Substring($current.Length)
                    }
                    & $push $target
                    continue
                }
                $missing = -not ([IO.Directory]::Exists($next) -or [IO.File]::Exists($next))
            }
            $current = $next
        }
    } catch {
        return $null
    }
    return $current
}

# Whether $Path, links followed, is $Root or lies under it. False when either
# cannot be resolved.
function Test-UnderDirectory([string]$Path, [string]$Root) {
    $resolvedRoot = Resolve-CanonicalPath $Root
    $resolved = Resolve-CanonicalPath $Path
    if (-not $resolvedRoot -or -not $resolved) { return $false }
    $comparison = if ($IsWindows) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    $separator = [IO.Path]::DirectorySeparatorChar
    return ($resolved.TrimEnd('/', '\') + $separator).StartsWith($resolvedRoot.TrimEnd('/', '\') + $separator, $comparison)
}

# Why the script may not read $Path from the extracted candidate $Root, or
# $null when it may. git archive keeps a committed symlink, so the candidate's
# compose.yml could point at a compose file elsewhere on the host, whose inline
# settings would then pass the allowlist unseen. No part of the path from $Root
# down may be a link, and it must be a regular file that resolves under $Root.
function Test-CandidateFile([string]$Path, [string]$Root) {
    $relative = [IO.Path]::GetRelativePath($Root, $Path)
    $current = $Root
    $parts = @('.') + @($relative.Split([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)))
    foreach ($part in $parts) {
        $current = [IO.Path]::Combine($current, $part)
        $shown = [IO.Path]::GetRelativePath($Root, $current)
        if ($null -ne [IO.FileInfo]::new($current).LinkTarget) {
            if ($shown -eq $relative) { return "$relative is a symbolic link, which the guard does not follow" }
            return "${relative}: $shown is a symbolic link, which the guard does not follow"
        }
    }
    if ($relative.StartsWith('..') -or -not [IO.File]::Exists($Path) -or -not (Test-UnderDirectory $Path $Root)) {
        return "$relative is not a regular file in the candidate source"
    }
    return $null
}

# What the resolved compose config may hold. Test-DisposableStorage refuses
# every other key, so a key a later candidate needs is judged and added here
# first rather than passing because nothing looked at it.
$KnownTopLevelKeys = @('name', 'services', 'volumes', 'networks')
$KnownServiceKeys = @('build', 'command', 'cpus', 'depends_on', 'entrypoint', 'environment', 'expose', 'extra_hosts',
    'healthcheck', 'image', 'init', 'ipc', 'mem_limit', 'mem_reservation', 'network_mode', 'networks', 'pid', 'ports',
    'pull_policy', 'restart', 'security_opt', 'stop_grace_period', 'stop_signal', 'tmpfs', 'user', 'volumes', 'working_dir')
# Keys a service could reach the host or another container's storage with,
# named so the refusal says why.
$RefusedServiceKeys = [ordered]@{
    volumes_from = "inherits another container's mounts"
    devices      = 'maps host devices into the container'
    privileged   = "gives the container the host's devices"
    cap_add      = 'adds kernel capabilities'
    userns_mode  = "shares the host's user namespace"
    extends      = 'pulls in a service the guard has not read'
    env_file     = "reads a host file into the container's environment"
}
$KnownVolumeKeys = @('name', 'driver', 'driver_opts', 'external')
$KnownNetworkKeys = @('name', 'driver', 'ipam')
# Per mount type, the keys a mount and its type's options may set.
$KnownMountKeys = @{
    bind   = @('type', 'source', 'target', 'read_only', 'bind')
    volume = @('type', 'source', 'target', 'read_only', 'volume')
    tmpfs  = @('type', 'target', 'read_only', 'tmpfs')
}
$KnownMountOptions = @{
    bind   = @('create_host_path')
    volume = @('nocopy', 'subpath')
    tmpfs  = @('size', 'mode')
}

function Get-PropNames($Object) {
    if ($Object -isnot [Management.Automation.PSCustomObject]) { return @() }
    return @($Object.PSObject.Properties | ForEach-Object { $_.Name })
}

function Test-ProjectName([string]$Name, [string]$Project) {
    return $Name.StartsWith("$Project-") -or $Name.StartsWith("${Project}_")
}

# Problems with one service's mounts. A bind's source, links followed, must be
# under the work directory, read-only or not: a read-only bind of the Docker
# socket still drives the daemon. A volume mount must name a project volume
# declared at the top level, which Test-DisposableStorage checks; an anonymous
# one is refused. tmpfs is memory only.
function Test-ServiceMounts([string]$Service, $Mounts, [string[]]$DeclaredVolumes, [string]$WorkDir) {
    foreach ($mount in @($Mounts)) {
        if ($null -eq $mount) { continue }
        $type = [string](Get-Prop $mount 'type')
        if (-not $KnownMountKeys.ContainsKey($type)) {
            "service $Service volumes: mount type '$type' is not bind, volume or tmpfs"
            continue
        }
        foreach ($key in Get-PropNames $mount) {
            if ($KnownMountKeys[$type] -cnotcontains $key) { "service $Service volumes: a $type mount sets $key" }
        }
        foreach ($key in Get-PropNames (Get-Prop $mount $type)) {
            if ($KnownMountOptions[$type] -cnotcontains $key) { "service $Service volumes: a $type mount sets ${type}.$key" }
        }
        $source = [string](Get-Prop $mount 'source')
        if ($type -eq 'bind' -and -not (Test-UnderDirectory $source $WorkDir)) {
            "service $Service volumes: bind $source is outside the work directory"
        } elseif ($type -eq 'volume' -and $DeclaredVolumes -cnotcontains $source) {
            "service $Service volumes: volume '$source' is not a declared project volume"
        }
    }
}

# Problems with the resolved compose config, as an allowlist: only the keys
# listed above, and of storage only new local project volumes, binds under the
# work directory and tmpfs. `down --volumes` deletes what compose resolves,
# restore replaces /receipts and teardown empties the backup tree, so anything
# else could reach another stack's storage: volumes_from inherits a production
# container's mounts, and a host namespace, device or capability reaches the
# host itself. A prefixed volume name proves nothing about where the data
# lives: an external volume, another driver or driver_opts (type=none, o=bind,
# device=...) can put real storage behind it, and a volume that exists already
# may be one, so only a new local volume without options counts as disposable.
# Built images must carry the project's name, so a build never retags a
# production image that teardown would then leave behind. A host file the
# candidate names is read before any container runs: a build's context and
# Dockerfile, links followed, must be under the work directory, and env_file
# is refused outright.
function Test-DisposableStorage($Config, [string]$Project, [string]$WorkDir, [string[]]$ExistingVolumes = @()) {
    foreach ($key in Get-PropNames $Config) {
        if ($KnownTopLevelKeys -cnotcontains $key) { "top-level ${key}: not a key the guard knows" }
    }
    $volumes = Get-Prop $Config 'volumes'
    foreach ($volume in @(if ($volumes) { $volumes.PSObject.Properties })) {
        $name = [string](Get-Prop $volume.Value 'name')
        if (-not (Test-ProjectName $name $Project)) { "volume $($volume.Name) is named $name" }
        if (Get-Prop $volume.Value 'external') { "volume $($volume.Name) is external" }
        $driver = Get-Prop $volume.Value 'driver'
        if ($null -ne $driver -and [string]$driver -ne 'local') { "volume $($volume.Name) uses driver $driver" }
        $options = Get-Prop $volume.Value 'driver_opts'
        if ($null -ne $options -and @(Get-PropNames $options).Count -gt 0) {
            "volume $($volume.Name) sets driver_opts"
        }
        foreach ($key in Get-PropNames $volume.Value) {
            if ($KnownVolumeKeys -cnotcontains $key) { "volume $($volume.Name) sets $key" }
        }
        if ($name -and $ExistingVolumes -ccontains $name) { "volume $($volume.Name) ($name) already exists" }
    }
    $declaredVolumes = @(Get-PropNames $volumes)
    $networks = Get-Prop $Config 'networks'
    foreach ($network in @(if ($networks) { $networks.PSObject.Properties })) {
        $name = [string](Get-Prop $network.Value 'name')
        if (-not (Test-ProjectName $name $Project)) { "network $($network.Name) is named $name" }
        $driver = Get-Prop $network.Value 'driver'
        if ($null -ne $driver -and [string]$driver -ne 'bridge') { "network $($network.Name) uses driver $driver" }
        if (@(Get-PropNames (Get-Prop $network.Value 'ipam')).Count -gt 0) { "network $($network.Name) sets ipam" }
        foreach ($key in Get-PropNames $network.Value) {
            if ($KnownNetworkKeys -cnotcontains $key) { "network $($network.Name) sets $key" }
        }
    }
    $declaredNetworks = @(Get-PropNames $networks)
    $services = Get-Prop $Config 'services'
    foreach ($service in @(if ($services) { $services.PSObject.Properties })) {
        $name = $service.Name
        $spec = $service.Value
        foreach ($key in Get-PropNames $spec) {
            if ($RefusedServiceKeys.Contains($key)) {
                "service $name ${key}: $($RefusedServiceKeys[$key])"
            } elseif ($KnownServiceKeys -cnotcontains $key) {
                "service $name ${key}: not a key the guard knows"
            }
        }
        foreach ($key in @('pid', 'ipc', 'network_mode')) {
            $mode = [string](Get-Prop $spec $key)
            if ($mode -eq 'host' -or $mode.StartsWith('container:')) { "service $name ${key}: $mode shares a namespace outside the project" }
        }
        foreach ($option in @(Get-Prop $spec 'security_opt')) {
            if ($null -ne $option -and [string]$option -notin @('no-new-privileges', 'no-new-privileges:true')) {
                "service $name security_opt: $option is not allowed"
            }
        }
        $attached = Get-Prop $spec 'networks'
        foreach ($network in @(if ($attached) { $attached.PSObject.Properties })) {
            if ($declaredNetworks -cnotcontains $network.Name) { "service $name networks: $($network.Name) is not a project network" }
            if ($null -ne $network.Value) { "service $name networks: $($network.Name) sets attachment options" }
        }
        $build = Get-Prop $spec 'build'
        if ($null -ne $build) {
            foreach ($key in Get-PropNames $build) {
                if ($key -cnotin @('context', 'dockerfile', 'args')) { "service $name build: sets $key" }
            }
            $context = [string](Get-Prop $build 'context')
            if (-not (Test-UnderDirectory $context $WorkDir)) { "service $name build: context $context is outside the work directory" }
            # The builder reads the Dockerfile wherever it is: compose keeps the
            # path as written, relative to the context unless it is absolute.
            $dockerfile = [string](Get-Prop $build 'dockerfile')
            if (-not $dockerfile) { $dockerfile = 'Dockerfile' }
            $effective = if ([IO.Path]::IsPathFullyQualified($dockerfile)) { $dockerfile } else { [IO.Path]::Combine($context, $dockerfile) }
            if (-not (Test-UnderDirectory $effective $WorkDir)) { "service $name build: dockerfile $dockerfile is outside the work directory" }
            $image = Get-Prop $spec 'image'
            if ($null -ne $image -and -not ([string]$image).StartsWith("$Project-")) { "service $name image: $image is not a $Project- image" }
        }
        Test-ServiceMounts $name (Get-Prop $spec 'volumes') $declaredVolumes $WorkDir
    }
}

# Compose resolves include and extends before `config` prints the model, so
# the model cannot show that the candidate's compose.yml pulled in a file the
# guard never read. Nor can it be trusted to show env_file: before Compose 5,
# `config` reads every env_file even with --no-env-resolution and drops the
# key from what it prints. compose.yml is refused if it names any of the three
# anywhere, in any case, or holds a backslash or a !, the only ways YAML spells
# a key without its letters ("\x69nclude", or a !!binary key). This runs
# before `config`, so no env_file is read.
function Test-ComposeSource([string]$Path) {
    $text = [IO.File]::ReadAllText($Path)
    foreach ($key in @('include', 'extends')) {
        if ($text -match $key) { "compose.yml ${key}: pulls in compose files the guard has not read" }
    }
    if ($text -match 'env_file') { "compose.yml env_file: $($RefusedServiceKeys['env_file'])" }
    if ($text.Contains('\')) { 'compose.yml: a backslash escape could spell include, extends or env_file' }
    if ($text.Contains('!')) { 'compose.yml: a YAML tag could spell include, extends or env_file' }
}

function ConvertFrom-EnvValue([string]$Raw) {
    # Compose env-file rules, as in ops/post-deploy-check.ps1.
    $text = $Raw.Trim()
    if ($text -match "^'([^']*)'") { return $Matches[1] }
    if ($text -match '^"((?:[^"\\]|\\.)*)"') { return $Matches[1] -replace '\\(["\\])', '$1' }
    return ($text -replace '\s+#.*$', '').Trim()
}

function Read-EnvValue([string]$Path, [string]$Key) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $value = $null
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^\s*(?:export\s+)?$([regex]::Escape($Key))\s*=(.*)$") {
            $value = ConvertFrom-EnvValue $Matches[1]
        }
    }
    return $value
}

# Of the file names in the nightly directory, the newest dump with a manifest
# and signature beside it. restore.sh checks the signature and the digests
# before it restores anything.
function Find-NewestSignedDump([string[]]$Names) {
    $dumps = @($Names | Where-Object { $_ -cmatch '^financial_planner_[A-Za-z0-9_-]+\.dump$' } | Sort-Object -Descending)
    foreach ($dump in $dumps) {
        $stem = $dump.Substring(0, $dump.Length - '.dump'.Length)
        if ($Names -ccontains "$stem.sha256" -and $Names -ccontains "$stem.sha256.sig") { return $dump }
    }
    return $null
}

function Get-FreePort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

# Runs docker with only $DockerEnvNames and DOCKER_* in its environment. Output
# is returned, never printed; stderr is dropped because restore and migrate
# errors can quote row values.
function Invoke-Docker([string[]]$Arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new('docker')
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.Environment.Clear()
    foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $name = [string]$entry.Key
        if ($DockerEnvNames -contains $name -or @($DockerEnvPrefixes | Where-Object { $name.StartsWith($_) }).Count -gt 0) {
            $info.Environment[$name] = [string]$entry.Value
        }
    }
    try {
        $process = [Diagnostics.Process]::Start($info)
    } catch {
        return [pscustomobject]@{ ExitCode = -1; Output = @() }
    }
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    [void]$stderr.Result
    $lines = @($stdout.Result -split "`r?`n" | Where-Object { $_ -ne '' })
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = $lines }
}

# Runs docker inside the isolated container against its own daemon. There is
# no fallback: until this run has created that container, nothing may talk to
# the candidate's project.
function Invoke-Isolated([string[]]$Arguments) {
    if (-not $script:IsolatedId) { throw 'the isolated daemon is not running, so nothing may talk to the candidate''s project' }
    return Invoke-Docker (@('exec', '-e', "DOCKER_HOST=$IsolatedSocket", $script:IsolatedId, 'docker') + $Arguments)
}

# Every compose command for the candidate runs in the isolated container, so a
# service the allowlist never saw (one in an inactive profile, say) is built
# and run only there, from files only there.
function Invoke-Compose([string[]]$Arguments) {
    return Invoke-Isolated (@('compose', '-p', $script:Project, '-f', $script:ComposeFile, '--env-file', $script:EnvFile) + $Arguments)
}

# Runs $Block with a `docker` first on PATH that runs each command inside the
# isolated container, for the helpers this script starts (post-deploy-check.ps1,
# prod_signals.py), then restores PATH and removes it. The shim lives in a new
# directory of its own, never in the work directory the candidate can write.
function Invoke-WithIsolatedDocker([scriptblock]$Block) {
    $real = @(Get-Command docker -CommandType Application -ErrorAction Stop)[0].Source
    $bin = [IO.Directory]::CreateTempSubdirectory('fp-rehearsal-bin-').FullName
    $shim = Join-Path $bin 'docker'
    "#!/bin/sh`nexec '$real' exec -e 'DOCKER_HOST=$IsolatedSocket' '$($script:IsolatedId)' docker `"`$@`"`n" |
        Set-Content -LiteralPath $shim -NoNewline -Encoding utf8NoBOM
    [IO.File]::SetUnixFileMode($shim, [IO.UnixFileMode]'UserRead, UserWrite, UserExecute')
    $path = $env:PATH
    $env:PATH = $bin + [IO.Path]::PathSeparator + $path
    try {
        return & $Block
    } finally {
        $env:PATH = $path
        Remove-Item -LiteralPath $bin -Recurse -Force
    }
}

function Get-IsolatedNames {
    return [pscustomobject]@{ Container = "$($script:Project)-isolated"; Volume = "$($script:Project)-isolated-docker" }
}

# Starts the isolated daemon on the host's Docker with the script's own fixed
# arguments: the pinned image, a new data volume, and the work directory at its
# own path (compose names bind sources by their host paths). Nothing from the
# candidate reaches this command. A container or volume of that name refuses
# the run rather than being reused or, later, removed: it may be another
# rehearsal's under the same -ProjectName. Teardown removes only what this run
# created. It waits until the daemon answers and reports itself rootless.
function Start-IsolatedDaemon {
    Assert-RehearsalProjectName $script:Project
    $names = Get-IsolatedNames
    foreach ($listing in @(@('container', 'ls', '-a', '-q', '--filter', "name=^/$($names.Container)$"),
            @('volume', 'ls', '-q', '--filter', "name=^$($names.Volume)$"))) {
        $found = Invoke-Docker $listing
        if ($found.ExitCode -ne 0) { throw 'cannot list containers and volumes on the host''s Docker' }
        if (@($found.Output).Count -gt 0) {
            throw "$($names.Container) or its volume already exists; another rehearsal may be running as $($script:Project)"
        }
    }
    $created = Invoke-Docker @('create', '--name', $names.Container,
        '--network', 'host',
        '--security-opt', 'apparmor=rootlesskit', '--security-opt', 'seccomp=unconfined',
        '--security-opt', 'systempaths=unconfined', '--device', '/dev/net/tun',
        '-v', "$($names.Volume):/home/rootless/.local/share/docker",
        '-v', "$($script:WorkDir):$($script:WorkDir)",
        $IsolatedImage, 'dockerd', "--host=$IsolatedSocket")
    $id = [string]@($created.Output)[-1]
    if ($created.ExitCode -ne 0 -or $id -notmatch '^[0-9a-f]{64}$') { throw 'creating the isolated daemon failed' }
    $script:IsolatedId = $id
    $script:IsolatedVolumeOwned = $true
    if ((Invoke-Docker @('start', $script:IsolatedId)).ExitCode -ne 0) {
        throw 'the isolated daemon did not start; docs/deployment.md lists what it needs'
    }
    $deadline = (Get-Date).AddSeconds($IsolatedStartSeconds)
    while ($true) {
        $info = Invoke-Isolated @('info', '--format', '{{range .SecurityOptions}}{{println .}}{{end}}')
        if ($info.ExitCode -eq 0) {
            if (@($info.Output) -notcontains 'name=rootless') { throw 'the isolated daemon is not rootless' }
            return
        }
        if ((Get-Date) -ge $deadline) {
            throw "the isolated daemon did not answer within $IsolatedStartSeconds seconds (it runs as uid 1000 and needs the work directory)"
        }
        Start-Sleep -Seconds 2
    }
}

# Removes the isolated container this run created, and with it every
# container, image and volume inside, then its data volume, and confirms on
# the host's Docker that neither remains. A failed removal or a listing that
# cannot be read is a failure, never taken for "gone".
function Stop-IsolatedDaemon {
    Assert-RehearsalProjectName $script:Project
    $names = Get-IsolatedNames
    $before = $script:Failures.Count
    if ($script:IsolatedId -and (Invoke-Docker @('rm', '--force', '--volumes', $script:IsolatedId)).ExitCode -ne 0) {
        Add-Failure "teardown: could not remove the isolated daemon $($names.Container)"
    }
    if ($script:IsolatedVolumeOwned -and (Invoke-Docker @('volume', 'rm', '--force', $names.Volume)).ExitCode -ne 0) {
        Add-Failure "teardown: could not remove the isolated daemon's volume $($names.Volume)"
    }
    $checks = @()
    if ($script:IsolatedId) { $checks += , @('container', 'ls', '-a', '-q', '--filter', "id=$($script:IsolatedId)") }
    if ($script:IsolatedVolumeOwned) { $checks += , @('volume', 'ls', '-q', '--filter', "name=^$($names.Volume)$") }
    foreach ($listing in $checks) {
        $left = Invoke-Docker $listing
        if ($left.ExitCode -ne 0) {
            Add-Failure "teardown: cannot confirm that the isolated daemon $($names.Container) and its volume are gone"
        } elseif (@($left.Output).Count -gt 0) {
            Add-Failure "teardown: the isolated daemon $($names.Container) or its volume remains"
        }
    }
    if ($script:Failures.Count -eq $before) {
        Write-Pass "teardown: the isolated daemon is gone, and every container, volume and image of $($script:Project) with it"
    }
}

# Runs the pinned image as root on the host's Docker, with no network and
# $Mounts read-only except the destination, for the two steps that need to
# read root-only backups. --mount fails on a missing source rather than
# creating it.
function Invoke-BackupHelper([string[]]$Mounts, [string[]]$Command) {
    $arguments = @('run', '--rm', '--network', 'none', '--user', '0:0', '--entrypoint', 'sh')
    foreach ($mount in $Mounts) { $arguments += @('--mount', $mount) }
    return Invoke-Docker ($arguments + @($IsolatedImage, '-c') + $Command)
}

# The file names in the nightly directory, which the backup scheduler's umask
# leaves readable by root alone.
function Get-NightlyNames([string]$Nightly) {
    $listed = Invoke-BackupHelper @("type=bind,source=$Nightly,target=/nightly,readonly") @('ls -1A /nightly')
    if ($listed.ExitCode -ne 0) { throw 'cannot list BACKUP_DIR/nightly' }
    return @($listed.Output)
}

# Copies the chosen dump, its receipts archive, manifest and signature, and
# the public signing key into the work directory, owned by the operator, so
# the isolated daemon needs no host directory but that one.
function Copy-NightlyDump([string]$Nightly, [string]$Dump, [string]$PublicKey, [string]$Destination) {
    $stem = $Dump.Substring(0, $Dump.Length - '.dump'.Length)
    $owner = "$((& id -u | Out-String).Trim()):$((& id -g | Out-String).Trim())"
    $copy = Invoke-BackupHelper @("type=bind,source=$Nightly,target=/nightly,readonly",
        "type=bind,source=$PublicKey,target=/signing.pub,readonly", "type=bind,source=$Destination,target=/input") @(
        'for f in /nightly/"$1".*; do cp -- "$f" /input/; done && cp /signing.pub /input/backup-signing-key.pub && chown -R "$2" /input',
        'copy', $stem, $owner)
    if ($copy.ExitCode -ne 0) { throw 'copying the nightly dump into the work directory failed' }
}

# The status `docker compose ls` gives a project, such as running(5), or absent.
function Get-ProjectStatus([string]$Name) {
    $listed = Invoke-Docker @('compose', 'ls', '--all', '--format', 'json')
    if ($listed.ExitCode -ne 0) { throw 'docker compose ls failed' }
    foreach ($project in @(($listed.Output -join "`n") | ConvertFrom-Json)) {
        if ($project.Name -eq $Name) { return [string]$project.Status }
    }
    return 'absent'
}

function Get-ContainerState([string]$Service) {
    $ids = Invoke-Compose @('ps', '--all', '-q', $Service)
    $id = @($ids.Output)[0]
    if (-not $id) { return [pscustomobject]@{ Id = $null; Status = 'absent'; Restarts = 0; Health = 'none' } }
    $inspect = Invoke-Isolated @('inspect', '--format', '{{.State.Status}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}', $id)
    $parts = @(@($inspect.Output)[0] -split ' ')
    if ($inspect.ExitCode -ne 0 -or $parts.Count -ne 3) { return [pscustomobject]@{ Id = $id; Status = 'unknown'; Restarts = 0; Health = 'none' } }
    return [pscustomobject]@{ Id = $id; Status = $parts[0]; Restarts = [int]$parts[1]; Health = $parts[2] }
}

# Healthy, or why not: a stopped or restarting container is final, since
# start-production.sh exits when migrate fails and Docker starts it again.
function Wait-AppHealthy([int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($true) {
        $state = Get-ContainerState 'app'
        if ($state.Status -eq 'running' -and $state.Restarts -eq 0 -and $state.Health -eq 'healthy') { return $state }
        if ($state.Status -ne 'running' -or $state.Restarts -gt 0) { return $state }
        if ((Get-Date) -ge $deadline) { return $state }
        Start-Sleep -Seconds 3
    }
}

function Write-TracebackTypes([string]$ContainerId, [datetime]$Since) {
    $sinceText = $Since.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", [Globalization.CultureInfo]::InvariantCulture)
    $helper = Join-Path $RepoRoot 'scripts/prod_signals.py'
    $result = Invoke-WithIsolatedDocker {
        $lines = & $Python $helper tracebacks --container $ContainerId --since $sinceText 2>$null
        [pscustomobject]@{ Code = $LASTEXITCODE; Output = $lines }
    }
    $output = $result.Output
    if ($result.Code -ne 0 -or -not $output) { return }
    $counts = ($output -join "`n") | ConvertFrom-Json
    if ($counts.tracebacks -gt 0) {
        $types = ($counts.types.PSObject.Properties | ForEach-Object { "$($_.Name) x$($_.Value)" }) -join ', '
        Write-Host "     app logged $($counts.tracebacks) traceback(s): $types"
    }
}

function Get-RowCounts {
    $result = Invoke-Compose @('run', '--rm', '-T', 'backup', '/opt/financial-planner/row-counts.sh')
    if ($result.ExitCode -ne 0) { throw 'row counts: row-counts.sh failed' }
    return ConvertFrom-RowCounts $result.Output
}

function New-RehearsalEnvFile([string]$Path, [string]$BackupDir, [int]$Port, [string]$Sha) {
    # Dummy values only. The schedules name a time that is never near a
    # rehearsal, and the harness, chat and notices are off, so the restored copy
    # never syncs, calls out or sends anything. Encrypted fields stay sealed:
    # this is not the production FIELD_ENCRYPTION_KEY.
    @(
        "POSTGRES_PASSWORD=rehearsal-only-not-a-secret"
        "DJANGO_SECRET_KEY=rehearsal-only-not-a-secret"
        "DJANGO_ALLOWED_HOSTS=rehearsal.example.test,127.0.0.1,localhost"
        "DJANGO_CSRF_TRUSTED_ORIGINS=https://rehearsal.example.test"
        "FIELD_ENCRYPTION_KEY=cmVoZWFyc2FsLW9ubHktbm90LWEtcmVhbC1rZXktMzI="
        "BACKUP_DIR=$BackupDir"
        "APP_PORT=$Port"
        "GIT_SHA=$Sha"
        "POSTGRES_VOLUME_NAME=$($script:Project)-postgres-data"
        "RECEIPTS_VOLUME_NAME=$($script:Project)-receipts"
        "SIMPLEFIN_SYNC_CRON='0 4 1 1 *'"
        "BACKUP_CRON='0 4 1 1 *'"
        "AGENT_HARNESS_ALLOWED_ORIGINS="
        "AI_CHAT_LOCAL_ENABLED=false"
        "OPERATOR_USERNAMES="
    ) | Set-Content -LiteralPath $Path -Encoding utf8NoBOM
}

function Resolve-Candidate([string]$Revision) {
    $sha = & git -C $RepoRoot rev-parse --verify --quiet "$Revision^{commit}" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $sha) { throw "cannot resolve -Candidate $Revision to a commit in this checkout; run git fetch origin" }
    return ([string]@($sha)[0]).Trim()
}

function Invoke-Rehearsal {
    if (-not $Candidate) { throw 'pass -Candidate, the git revision to rehearse' }
    if ($DumpPath -and $ConfigPath) { throw 'pass -DumpPath or -ConfigPath, not both' }
    if (-not $DumpPath -and -not $ConfigPath) { throw 'pass -ConfigPath to rehearse the newest nightly dump, or -DumpPath for a synthetic one' }
    $script:Project = if ($ProjectName) { $ProjectName } else { $ProjectPrefix + [Guid]::NewGuid().ToString('N').Substring(0, 8) }
    Assert-RehearsalProjectName $script:Project
    $script:Sha = Resolve-Candidate $Candidate

    if ($DumpPath) {
        if (-not (Test-Path -LiteralPath $DumpPath -PathType Leaf)) { throw '-DumpPath is not a file' }
        $dump = (Resolve-Path -LiteralPath $DumpPath).Path
        $dumpKind = 'synthetic dump (no signed manifest)'
    } else {
        $backupDir = Read-EnvValue $ConfigPath 'BACKUP_DIR'
        if (-not $backupDir) { throw '-ConfigPath has no BACKUP_DIR' }
    }

    $docker = Invoke-Docker @('info', '--format', '{{.ServerVersion}}')
    if ($docker.ExitCode -ne 0) { throw 'Docker is not reachable; start it yourself, this script never does' }
    if (-not $DumpPath) {
        # The nightly directory is root-only, so a helper container lists it.
        $nightly = Join-Path $backupDir 'nightly'
        $dump = Find-NewestSignedDump (Get-NightlyNames $nightly)
        if (-not $dump) { throw 'no signed nightly dump in BACKUP_DIR/nightly; run a backup first' }
        $publicKey = Join-Path $backupDir 'signing/backup-signing-key.pub'
        $dumpKind = "nightly $dump"
    }
    $script:ProductionBefore = Get-ProjectStatus $ProductionProject
    Write-Pass "production: project $ProductionProject is $($script:ProductionBefore)"
    if ((Get-ProjectStatus $script:Project) -ne 'absent') { throw "project $($script:Project) already exists; remove it or pass another -ProjectName" }

    $port = if ($AppPort -gt 0) { $AppPort } else { Get-FreePort }
    $script:WorkDir = New-RehearsalWorkDir
    $script:WorkDirIdentity = Get-DirectoryIdentity $script:WorkDir
    if (-not $script:WorkDirIdentity) { throw 'cannot read the identity of the new work directory' }
    $source = Join-Path $script:WorkDir 'source'
    $backups = Join-Path $script:WorkDir 'backups'
    $inputs = Join-Path $script:WorkDir 'input'
    New-Item -ItemType Directory -Path $source, $backups, $inputs | Out-Null
    $archive = Join-Path $script:WorkDir 'source.tar'
    & git -C $RepoRoot archive --format=tar -o $archive $script:Sha 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'git archive of the candidate failed' }
    & tar -xf $archive -C $source 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'extracting the candidate source failed' }
    Remove-Item -LiteralPath $archive
    $script:ComposeFile = Join-Path $source 'compose.yml'
    $script:EnvFile = Join-Path $script:WorkDir 'rehearsal.env'
    New-RehearsalEnvFile $script:EnvFile $backups $port $script:Sha
    Write-Pass "candidate: $($script:Sha.Substring(0, 12)) as project $($script:Project) on 127.0.0.1:$port"

    # -f names this file alone, so no override or COMPOSE_FILE adds another, and
    # --env-file stops compose reading the candidate's .env: compose.yml is the
    # only candidate file read before teardown is armed.
    $linked = Test-CandidateFile $script:ComposeFile $source
    if ($linked) { throw "compose file the guard cannot vouch for: $linked" }
    $problems = @(Test-ComposeSource $script:ComposeFile)
    if ($problems.Count -gt 0) { throw "compose file the guard cannot vouch for: $($problems -join '; ')" }

    # The dump is copied in, so the isolated daemon gets no host directory but
    # the work directory. From here on every compose command goes to that
    # daemon; the host's Docker only starts and removes it.
    $name = [IO.Path]::GetFileName($dump)
    if ($DumpPath) {
        Copy-Item -LiteralPath $dump -Destination (Join-Path $inputs $name)
    } else {
        Copy-NightlyDump $nightly $name $publicKey $inputs
    }
    Start-IsolatedDaemon
    Write-Pass "isolated: rootless daemon $((Get-IsolatedNames).Container); compose for the candidate runs inside it, never against the host's Docker"

    # The allowlist below stays as a second line: it judges the model compose
    # prints, which leaves out a service in an inactive profile, so it is not
    # what keeps the candidate away from production. The isolated daemon is.
    # Without --no-env-resolution compose reads each service's env_file and
    # folds it into environment, dropping the key the guard refuses. Compose
    # before 5 does that regardless, which is why Test-ComposeSource refuses
    # env_file in the text first.
    $config = Invoke-Compose @('config', '--no-env-resolution', '--format', 'json')
    if ($config.ExitCode -ne 0) { throw 'docker compose config failed for the candidate' }
    # Absence from `compose ls` does not mean its volumes are new. One that
    # exists is refused, not reused, and never armed for `down --volumes`.
    $existing = Invoke-Isolated @('volume', 'ls', '-q')
    if ($existing.ExitCode -ne 0) { throw 'docker volume ls failed' }
    $problems = @(Test-DisposableStorage (($config.Output -join "`n") | ConvertFrom-Json) $script:Project $script:WorkDir @($existing.Output))
    if ($problems.Count -gt 0) { throw "storage outside the throwaway project: $($problems -join '; ')" }
    Write-Pass "storage: only new $($script:Project)-* volumes, bind mounts under the work directory and tmpfs"

    # From here the project may hold containers, volumes and images; teardown removes them.
    $script:Armed = $true
    if ((Invoke-Compose (@('build') + $BuiltServices)).ExitCode -ne 0) { throw 'building the candidate images failed' }
    $script:Built = $true
    Write-Pass "build: $($BuiltServices -join ', ')"
    if ((Invoke-Compose @('up', '-d', '--wait', '--wait-timeout', '180', 'db')).ExitCode -ne 0) { throw 'db did not report healthy' }

    $restore = @('run', '--rm', '-T')
    if ($DumpPath) {
        $restore += @('-v', "$(Join-Path $inputs $name):/rehearsal/${name}:ro", '-e', 'RESTORE_ALLOW_UNVERIFIED=1')
    } else {
        $restore += @('-v', "${inputs}:/rehearsal:ro",
            '-v', "$(Join-Path $inputs 'backup-signing-key.pub'):/rehearsal-signing.pub:ro", '-e', 'BACKUP_SIGNING_PUBLIC_KEY=/rehearsal-signing.pub')
    }
    $restore += @('backup', '/opt/financial-planner/restore.sh', "/rehearsal/$name")
    if ((Invoke-Compose $restore).ExitCode -ne 0) { throw "restore.sh refused or failed on the $dumpKind" }
    $before = Get-RowCounts
    if ($before.Count -eq 0) { throw 'the restored database has no tables' }
    Write-Pass "restore: $dumpKind, $($before.Count) tables"

    $deployStart = Get-Date
    if ((Invoke-Compose @('up', '-d', 'app')).ExitCode -ne 0) { throw 'starting the candidate app failed' }
    $app = Wait-AppHealthy $MigrateTimeoutSeconds
    if ($app.Health -ne 'healthy' -or $app.Restarts -gt 0) {
        Add-Failure "migrate: app is $($app.Status), health $($app.Health), RestartCount $($app.Restarts) after migrating"
        if ($app.Id) { Write-TracebackTypes $app.Id $deployStart }
        return
    }
    $after = Get-RowCounts
    $rows = @(Compare-RowCounts $before $after $ExpectCountChange)
    foreach ($row in $rows) {
        $line = Format-CountRow $row
        if ($row.Failed) { $script:Failures.Add("counts: $($row.Table) $($row.State)") }
        Write-Host $line
    }
    $script:Tables = $rows.Count
    $script:Migrations = [int]($after['django_migrations'] - $before['django_migrations'])
    Write-Pass "migrate: app healthy, $($script:Migrations) migration(s) applied"

    if ((Invoke-Compose @('up', '-d')).ExitCode -ne 0) {
        Add-Failure 'up: the full stack did not start (config-check or background)'
        return
    }
    if ((Invoke-Compose @('run', '--rm', '-T', 'backup', '/opt/financial-planner/backup.sh')).ExitCode -ne 0) {
        Add-Failure 'backup: backup.sh failed on the migrated database'
    } else {
        Write-Pass 'backup: the migrated database dumps and restores'
    }

    $reconcile = Invoke-Compose @('exec', '-T', 'app', 'python', 'manage.py', 'reconcile_balances', '--json')
    $summary = $null
    if ($reconcile.ExitCode -in @(0, 1)) {
        try { $summary = ($reconcile.Output -join "`n") | ConvertFrom-Json } catch { $summary = $null }
    }
    if ($null -eq $summary -or $null -eq (Get-Prop $summary 'counts')) {
        Add-Failure "reconcile: reconcile_balances --json exited $($reconcile.ExitCode) without a summary"
    } else {
        $counts = $summary.counts
        $parts = ($counts.PSObject.Properties | ForEach-Object { "$($_.Value) $($_.Name)" }) -join ', '
        $mismatches = [int](Get-Prop $counts 'mismatch')
        if ($mismatches -gt $KnownMismatchWindows) {
            Add-Failure "reconcile: $($summary.accounts) account(s): $parts; more than -KnownMismatchWindows $KnownMismatchWindows"
        } else {
            Write-Pass "reconcile: $($summary.accounts) account(s): $parts"
        }
    }

    $check = Join-Path $PSScriptRoot 'post-deploy-check.ps1'
    $arguments = @('-NoProfile', '-NonInteractive', '-File', $check,
        '-ProjectName', $script:Project,
        '-HealthUrl', "http://127.0.0.1:$port/health/",
        '-ExpectedSha', $script:Sha,
        '-Since', $deployStart.ToString('o'),
        '-StableSeconds', [string]$StableSeconds,
        '-StatusPath', (Join-Path $backups 'health/status'),
        '-Python', $Python)
    $result = Invoke-WithIsolatedDocker {
        $lines = & ([Environment]::ProcessPath) @arguments 2>$null
        [pscustomobject]@{ Code = $LASTEXITCODE; Output = $lines }
    }
    $code = $result.Code
    foreach ($line in @($result.Output)) { Write-Host "     post-deploy $line" }
    if ($code -ne 0) { Add-Failure "post-deploy: the check exited $code" } else { Write-Pass 'post-deploy: passed' }
}

function Invoke-Teardown {
    Assert-RehearsalProjectName $script:Project
    if ((Invoke-Compose @('down', '--volumes', '--remove-orphans', '--timeout', '10')).ExitCode -ne 0) {
        Add-Failure "teardown: docker compose down failed for $($script:Project)"
    }
    $backupImage = "$($script:Project)-backup"
    if ($script:Built -and (Invoke-Isolated @('image', 'inspect', $backupImage)).ExitCode -eq 0) {
        # The containers write the backup tree as root. Once they are gone, empty
        # it from a plain container (compose would recreate the receipts volume)
        # so no dump or audit file outlives the rehearsal.
        $backups = Join-Path $script:WorkDir 'backups'
        if (-not (Test-WorkDirIdentity)) {
            Add-Failure 'teardown: the work directory was replaced after it was created; left it unemptied'
        } elseif (Test-UnderDirectory $backups $script:WorkDir) {
            [void](Invoke-Isolated @('run', '--rm', '--network', 'none', '-v', "${backups}:/backups", '--entrypoint', 'sh',
                $backupImage, '-c', 'find /backups -mindepth 1 -delete'))
        } else {
            Add-Failure 'teardown: the backup directory no longer resolves inside the work directory; left it unemptied'
        }
    }
    # Its images, volumes and any container left go with the isolated daemon.
}

function Get-ErrorText($ErrorRecord) {
    # Messages thrown above are this script's own; anything else by type only.
    if ($ErrorRecord.Exception -is [Management.Automation.RuntimeException] -and $ErrorRecord.Exception.WasThrownFromThrowStatement) {
        return $ErrorRecord.Exception.Message
    }
    return $ErrorRecord.Exception.GetType().Name
}

# Dot-sourcing loads the functions above for tests/test_deploy_rehearsal.py.
if ($MyInvocation.InvocationName -eq '.') { return }

$script:Project = $null
$script:Sha = $null
$script:Armed = $false
$script:IsolatedId = $null
$script:IsolatedVolumeOwned = $false
$script:Built = $false
$script:WorkDir = $null
$script:WorkDirIdentity = $null
$script:ProductionBefore = $null
$script:Tables = 0
$script:Migrations = 0
try {
    Invoke-Rehearsal
} catch {
    Add-Failure "rehearsal: $(Get-ErrorText $_)"
} finally {
    if ($script:Armed) {
        try { Invoke-Teardown } catch { Add-Failure "teardown: $(Get-ErrorText $_)" }
    }
    if ($script:IsolatedId -or $script:IsolatedVolumeOwned) {
        try { Stop-IsolatedDaemon } catch { Add-Failure "teardown: $(Get-ErrorText $_)" }
    }
    if ($script:WorkDir -and (Test-Path -LiteralPath $script:WorkDir) -and -not (Test-WorkDirIdentity)) {
        Add-Failure "teardown: the work directory $($script:WorkDir) was replaced after it was created; left it in place"
    } elseif ($script:WorkDir -and (Test-Path -LiteralPath $script:WorkDir)) {
        try {
            Remove-Item -LiteralPath $script:WorkDir -Recurse -Force
        } catch {
            Add-Failure "teardown: could not remove the work directory $($script:WorkDir)"
        }
    }
}
if ($null -ne $script:ProductionBefore) {
    $productionAfter = try { Get-ProjectStatus $ProductionProject } catch { 'unreadable' }
    if ($productionAfter -eq $script:ProductionBefore) {
        Write-Pass "production: project $ProductionProject is still $productionAfter"
    } else {
        Add-Failure "production: project $ProductionProject went from $($script:ProductionBefore) to $productionAfter during the rehearsal"
    }
}
Write-Host (Format-Verdict $script:Failures.Count $script:Sha $script:Tables $script:Migrations)
if ($script:Failures.Count -gt 0) { exit 1 }
exit 0
