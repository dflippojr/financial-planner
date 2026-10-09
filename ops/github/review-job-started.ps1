# Runner-host policy, installed outside the checkout and runner application directory.
# Keep this allow-list in sync with the repository's default branch (currently main).
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

try {
    if ($env:GITHUB_REPOSITORY -cne 'dflippojr/financial-planner' -or
        $env:GITHUB_WORKFLOW_REF -cne 'dflippojr/financial-planner/.github/workflows/review.yml@refs/heads/main' -or
        $env:GITHUB_REF -cne 'refs/heads/main' -or
        $env:GITHUB_EVENT_NAME -cnotin @('pull_request_target', 'workflow_dispatch')) {
        throw 'job context is outside the review allow-list'
    }

    $event = Get-Content -LiteralPath $env:GITHUB_EVENT_PATH -Raw | ConvertFrom-Json
    if ($event.repository.full_name -cne 'dflippojr/financial-planner' -or
        $event.repository.default_branch -cne 'main') {
        throw 'event repository is outside the review allow-list'
    }
    if ($env:GITHUB_EVENT_NAME -ceq 'pull_request_target') {
        if ($event.pull_request.base.repo.full_name -cne 'dflippojr/financial-planner' -or
            $event.pull_request.base.ref -cne 'main' -or
            $event.pull_request.head.repo.full_name -cne 'dflippojr/financial-planner') {
            throw 'pull request is outside the review allow-list'
        }
    }
} catch {
    # Do not echo the payload, environment, or exception: those can contain private data.
    [Console]::Error.WriteLine('Review runner refused job: context is missing, invalid, or not allowed.')
    exit 1
}

Write-Output 'Review runner accepted the base-repository review workflow on main.'
exit 0
