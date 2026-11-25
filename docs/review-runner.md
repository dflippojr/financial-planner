# Linux review runner

Reviews run on a Linux self-hosted GitHub Actions runner. The routing source of
truth is [`.github/workflows/review.yml`](../.github/workflows/review.yml):
`runs_on` is `["self-hosted","Linux","X64","financial-planner-review"]`.
The caller pins both the reusable workflow and its tooling to `review-v4` in
`dflippojr/agent-harness`. For provisioning and shared reviewer configuration,
see [Agent Harness CI/CD, Adopting the review workflow](https://github.com/dflippojr/agent-harness/blob/main/docs/CI-CD.md).

Host installation, service configuration and the job-started hook live outside
this checkout and are managed by the owner. This repository does not ship a
Linux runner installer. Do not use the retained `ops/github/*.ps1` files to
provision it: they are legacy artifacts awaiting removal in
[#790](https://github.com/dflippojr/financial-planner/issues/790) after the
owner confirms a real Linux deploy. A repository edit cannot update a running
runner; do not restart or re-register it as part of an application issue.

## Required controls

- Keep GitHub **Settings → Actions → General → Fork pull request workflows →
  Require approval for all external contributors** selected (API value
  `all_external_contributors`).
- The caller uses `pull_request_target` and `workflow_dispatch`. It skips fork
  heads and Dependabot PRs on open; the shared reviewer also refuses fork PRs.
- The owner-managed `ACTIONS_RUNNER_HOOK_JOB_STARTED` allow-list must permit
  only `dflippojr/financial-planner/.github/workflows/review.yml@refs/heads/main`,
  `GITHUB_REF=refs/heads/main`, and those two events. Validate the event's
  repository and default branch; PR events require base `main` and both base
  and head in this repository. Missing or malformed context must fail before
  steps run. Reusable jobs retain the caller's workflow context.
- Use a dedicated non-root Linux account. Keep runner and hook directories
  writable only by that account and operators, with the hook outside the PR
  checkout. Keep production data, the Docker socket and the owner's credentials
  inaccessible to it. Labels route jobs; they do not authorize them.
- Prefer one-job ephemeral registration and a fresh installation for each job.
  Preserve `_diag` logs in operator-controlled storage, confirm the old runner
  exited, then clean its workspace and provision with a verified runner archive
  and fresh registration token. Keep tokens out of source, history and logs.
  Ephemeral registration does not erase files or credentials or create a fresh
  host; a disposable host provides stronger isolation.

These are the required host controls, not evidence of their installed state.
The hook is defense in depth: the reviewer reads untrusted PR text with shell
access. The owner verifies the effective hook path and permissions, non-root
service identity, accepted/rejected synthetic contexts and **Set up runner** hook
log when applying host changes. If the default branch changes, update its
explicit allow-list before provisioning replacements.

## Operator checks

From a trusted checkout, inspect the caller and list repository runners:

```bash
sed -n '1,120p' .github/workflows/review.yml
gh api repos/dflippojr/financial-planner/actions/runners \
  --jq '.runners[] | {name, os, status, busy, labels: [.labels[].name]}'
```

Expect an online Linux runner with all four routing labels when a review is
ready to run; an ephemeral runner may disappear after its job. Compare the
actual service and hook with the owner's provisioning record rather than
assuming these labels prove isolation. Dispatch a review only for the intended
PR: `gh workflow run review.yml -f pr_number=N -f mode=full` (replace `N`).

References: [GitHub job hooks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts)
and [ephemeral registration](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#ephemeral-runners-for-autoscaling).
