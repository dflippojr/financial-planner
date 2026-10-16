# Throwaway for issue #528: proves the gate check goes red when a needed job
# fails. Reverted in the next commit.


def test_gate_goes_red_when_a_needed_job_fails():
    assert False, "deliberate failure for the gate proof"
