"""Static checks of the workflow files that judge a pull request; nothing here calls GitHub."""

from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def _code(name):
    # Drop comment lines, so a trigger or key that a comment names doesn't count.
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")) + "\n"


def _header(workflow):
    return workflow.split("\njobs:\n", 1)[0] + "\n"


def _jobs(workflow):
    body = workflow.split("\njobs:\n", 1)[1]
    return dict(re.findall(r"^  ([\w-]+):\n((?:(?:    .*)?\n)*)", body, re.M))


def _permissions(header):
    block = re.search(r"^permissions:\n((?:  .+\n)+)", header, re.M)
    assert block, header
    return set(block.group(1).splitlines())


def _guard_sha():
    match = re.search(r"uses: dflippojr/\.github/\.github/workflows/gate-guard\.yml@([0-9a-f]{40})\n",
                      _code("ci.yml"))
    assert match, "ci.yml no longer calls the pinned gate guard"
    return match.group(1)


def test_trusted_gate_guard_runs_from_the_default_branch_and_runs_no_pull_request_code():
    # ci.yml runs on pull_request, so its gate-guard job comes from the pull request's head, which can swap it for a
    # green no-op. pull_request_target reads this caller from main; the called workflow fetches the pull request's
    # commits as git objects, never checks them out, and counts the owner's label only after the newest push.
    target = _code("gate-guard-target.yml")
    header = _header(target)
    assert re.search(r"^on:\n  pull_request_target:\n    types: \[opened, synchronize, reopened, edited, labeled, "
                     r"unlabeled\]\n\n", header, re.M), header
    assert "pull_request:" not in target and "workflow_dispatch" not in target and "push:" not in target
    assert _permissions(header) == {"  contents: read", "  issues: read", "  pull-requests: read",
                                    "  checks: write"}
    assert "group: gate-guard-target-${{ github.event.pull_request.head.sha }}\n" in header
    assert "cancel-in-progress: true\n" in header
    jobs = _jobs(target)
    assert set(jobs) == {"judge"}
    sha = _guard_sha()
    assert f"    uses: dflippojr/.github/.github/workflows/gate-guard-target.yml@{sha}\n" in jobs["judge"]
    assert f"      guard-ref: {sha}\n" in jobs["judge"]
    for unsafe in ("steps:", "run:", "actions/checkout", "secrets", "self-hosted", "runs-on",
                   "github.event.pull_request.title", "github.head_ref"):
        assert unsafe not in jobs["judge"], unsafe


def test_ci_keeps_building_on_pull_request():
    # Only the guard moves to the default branch; the build and tests keep running the pull request's own code.
    header = _header(_code("ci.yml"))
    assert re.search(r"^  pull_request:\n", header, re.M)
    assert "pull_request_target" not in header


def test_review_caller_grants_every_permission_review_v3_requests():
    # A called workflow can't hold more than its caller grants, and review-v3 reads the issues a pull request
    # closes, the spec it reviews against.
    review = _code("review.yml")
    assert "uses: dflippojr/agent-harness/.github/workflows/review.yml@review-v3\n" in review
    assert "tooling_ref: review-v3\n" in review
    assert _permissions(_header(review)) == {"  contents: read", "  pull-requests: write", "  checks: write",
                                             "  issues: read"}
