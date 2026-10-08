# Source from maintenance scripts. All values below are fixed enums or UUIDs.
audit_python=${AUDIT_PYTHON:-python3}
audit_script=$(dirname "$0")/audit_journal.py
audit_actor=${OPERATOR_AUDIT_ACTOR:-operator}
case "$audit_actor" in operator|scheduler) ;; *) audit_actor=operator ;; esac
audit_run_id=$("$audit_python" -c 'import os, uuid; print(uuid.UUID(os.environ["OPERATOR_AUDIT_RUN_ID"]) if os.environ.get("OPERATOR_AUDIT_RUN_ID") else uuid.uuid4())' 2>/dev/null) || audit_run_id=""
export OPERATOR_AUDIT_RUN_ID=$audit_run_id

audit_line() {
  if [ -n "$audit_run_id" ]; then
    "$audit_python" "$audit_script" "$1" "$2" "$audit_run_id" --actor "$audit_actor" || echo 'Audit write gap: operator journal could not be recorded' >&2
  else
    echo 'Audit write gap: operator journal could not be recorded' >&2
  fi
  return 0
}

audit_end() {
  if [ "$1" -eq 0 ]; then audit_line "$audit_operation" succeeded
  else audit_line "$audit_operation" failed; fi
}
