# Windows review runner

The owner applies these host changes. A repository change does not update an
already running runner. Do not run this installer against the live runner or
restart it as part of an issue implementation.

## Controls

- In GitHub **Settings → Actions → General → Fork pull request workflows**, keep
  **Require approval for all external contributors** selected. The API reports
  this as `all_external_contributors`.
- `.github/workflows/review.yml` uses `pull_request_target` and
  `workflow_dispatch`; its job guard skips fork heads and Dependabot PRs. The
  shared reviewer also checks the PR before using its head as review context.
- `ops/github/review-job-started.ps1` is a host-side allow-list. It requires
  `dflippojr/financial-planner/.github/workflows/review.yml@refs/heads/main`,
  `GITHUB_REF=refs/heads/main`, and either `pull_request_target` or
  `workflow_dispatch`. It also checks the event's repository and default branch;
  PR events must have a base on main and both base and head in this repository.
  Missing or malformed context and every other job fail before workflow steps.
  Reusable workflow jobs retain the caller's workflow context; the allow-list
  names this repository's caller, not the shared Agent Harness implementation.

The hook is defense in depth, not a sandbox. Labels route jobs; they do not
provide authorization. The reviewer reads untrusted PR text with shell access.
A compromised host or permitted review job could alter files accessible to its
account. Keep the runner separate from production data, Docker administration,
and the owner's credentials. If the default branch changes, the owner must
update the hook's explicit `main` allow-list before provisioning replacements.

## Owner installation

1. Create a dedicated **standard (non-administrator)** Windows account for
   reviews. Sign in as that account and provision only the review tools and
   credentials it needs. Do not copy the owner's profile or financial data.
   Restrict the runner and hook directories' ACLs to this account and operators;
   other local users must not be able to replace the hook.
2. Copy these scripts from the trusted default branch. Use a new, empty runner
   directory and a separate hook directory outside the checkout. The installer
   verifies the pinned runner archive's SHA-256 and refuses `.runner` files that
   indicate an existing registration. It also refuses an elevated administrator
   token; the account must still be a standard account even when not elevated.
3. Obtain a fresh repository registration token through an authorized operator.
   Supply it in memory to the account running the installer; never save it in
   source, shell history, task arguments, or logs. For example, where that account
   has the required GitHub permission:

   ```powershell
   $token = gh api -X POST repos/dflippojr/financial-planner/actions/runners/registration-token --jq .token
   .\ops\github\install-runner.ps1 -Token $token
   Remove-Variable token
   ```

   Defaults are `D:\Agents\financial-planner-review`, runner name and label
   `financial-planner-review`, and task `FinancialPlanner-GitHubRunner-Review`.
   The installer copies the hook to `D:\Agents\financial-planner-review-hooks`
   and writes its absolute path as `ACTIONS_RUNNER_HOOK_JOB_STARTED` in the
   runner's `.env`. Other `.env` keys are preserved. Custom `-InstallDir`,
   `-HookDir`, `-Name`, and `-TaskName` allow separate pool members.
4. The hidden task starts immediately and at logon under this same account,
   with `RunLevel Limited` and an interactive logon. It does not install a
   system service or run as SYSTEM. The account must remain signed in.

## One-job lifecycle

Registration uses `--ephemeral`: GitHub assigns one job, then deregisters the
runner. The logon task cannot register it again and no token is stored for
automatic re-registration. An owner must provision another runner before the
next review; queued reviews wait for an available registration.

After each job, preserve `_diag` logs in operator-controlled storage outside
the disposable installation, confirm the runner process has exited, and remove
its old scheduled task. Clean the work directory and reprovision from trusted
scripts and a verified archive in a fresh directory with a fresh token. Keep
review credentials narrowly scoped. Ephemeral registration does **not** erase
the Windows profile, workspace, credentials, or logs and does not itself provide
a fresh host. A disposable host gives stronger isolation where available.

For an existing persistent runner, the owner must arrange its retirement and
replacement separately. This installer deliberately does not migrate or
re-register that runner in place.

## Verification

Run `python -m pytest tests/test_review_runner.py -q` with PowerShell available
(`pwsh`, or `powershell.exe` on Windows). The tests exercise accepted and rejected
synthetic event contexts and mock download, registration, and scheduled-task
operations; they never change the live runner. These script tests skip on hosts
without PowerShell. Verify the deployed `.env`, hook ACLs, limited task identity,
and the **Set up runner** hook log when the owner applies the change.

References: [GitHub job hooks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts)
and [ephemeral registration](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#ephemeral-runners-for-autoscaling).
