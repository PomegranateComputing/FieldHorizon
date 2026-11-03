"""
The vocabulary of DB engineering outcomes, shared by operations, recovery, the API and the UI.

Every terminal status comes with a machine-readable cause code; nothing ends
as a bare "failed". Live statuses describe an operation still in progress.
"""
from __future__ import annotations

# --- live -------------------------------------------------------------------------
RUNNING = "RUNNING"
PAUSED = "PAUSED"
CANCELLING = "CANCELLING"

# --- terminal: optimization operations ------------------------------------------------
RETAINED = "RETAINED"                      # change kept: every acceptance criterion passed
REVERTED = "REVERTED"                      # change measured, criteria failed, change removed (compensation)
REJECTED = "REJECTED"                      # every candidate refused by the policy; nothing changed
NO_CANDIDATE = "NO_CANDIDATE"              # the measured workload offered nothing to try; nothing changed
BLOCKED = "BLOCKED"                        # a precondition failed before any change; nothing changed
CANCELLED = "CANCELLED"                    # stopped by an operator; any change removed
FAILED_RECOVERED = "FAILED_RECOVERED"      # failed; the target was confirmed back to its prior state
FAILED_NEEDS_RECOVERY = "FAILED_NEEDS_RECOVERY"  # failed; the target could not be confirmed clean
RECOVERED_REVERTED = "RECOVERED_REVERTED"  # interrupted (crash, kill); brought back by a recovery run

# --- terminal: recovery runs -----------------------------------------------------------
RECOVERY_DONE = "RECOVERY_DONE"
RECOVERY_NOTHING = "RECOVERY_NOTHING_TO_DO"
RECOVERY_BLOCKED = "RECOVERY_BLOCKED"
RECOVERY_DEFERRED = "RECOVERY_DEFERRED"

LIVE = (RUNNING, PAUSED, CANCELLING)
TERMINAL = (RETAINED, REVERTED, REJECTED, NO_CANDIDATE, BLOCKED, CANCELLED, FAILED_RECOVERED, FAILED_NEEDS_RECOVERY,
            RECOVERED_REVERTED, RECOVERY_DONE, RECOVERY_NOTHING, RECOVERY_BLOCKED, RECOVERY_DEFERRED)
# Did the target end where it started? (RETAINED is the one intended difference.)
TARGET_UNCHANGED = (REVERTED, REJECTED, NO_CANDIDATE, BLOCKED, CANCELLED, FAILED_RECOVERED, RECOVERED_REVERTED)

# --- cause codes (operation-level; executor refusal and failure codes are added verbatim) --
CAUSE_ACCEPTED = "acceptance_passed"
CAUSE_ACCEPTANCE_FAILED = "acceptance_failed"
CAUSE_NO_CANDIDATE = "no_candidate"
CAUSE_POLICY_REJECTED = "policy_rejected_all"
CAUSE_AUTHORITY_REFUSED = "authority_refused"
CAUSE_UNRECOVERED_STATE = "unrecovered_state"
CAUSE_CANCELLED = "cancelled_by_operator"
CAUSE_INTERRUPTED = "interrupted"
CAUSE_ERROR = "unexpected_error"

# --- domain events -------------------------------------------------------------------
EVT_STARTED = "DbOperationStarted"
EVT_STEP_COMPLETED = "DbStepCompleted"
EVT_STEP_FAILED = "DbStepFailed"
EVT_CANDIDATE_EVALUATED = "DbCandidateEvaluated"
EVT_CHANGE_APPLIED = "DbChangeApplied"
EVT_DECISION_RECORDED = "DbDecisionRecorded"
EVT_CHANGE_REVERTED = "DbChangeReverted"
EVT_COMPLETED = "DbOperationCompleted"
EVT_FAILED = "DbOperationFailed"
EVT_PAUSED = "DbOperationPaused"
EVT_RESUMED = "DbOperationResumed"
EVT_CONTROL_REQUESTED = "DbControlRequested"
EVT_RECOVERED = "DbOperationRecovered"
EVT_RECOVERY_COMPLETED = "DbRecoveryCompleted"
EVT_HEARTBEAT = "DbOperationHeartbeat"
