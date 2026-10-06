"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner GitLab workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing `formatted_output`.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT the
framework `_extra_security_gate_output` hook - the framework gate methods are
@final on FunctionNode and the SDK auto-wraps `_extra_` hooks (which breaks
the .invoke() chain), so domain checks live in a module-level helper invoked
inline. The same helper re-scans the structured keys surfaced by
GitLabIssueMRAgent.get_output() (structured product, fail-closed - docs/02
"Structured Output").

Output containment: the raw `gitlab_payload` request dict is NEVER surfaced
to the caller - only scalar evidence fields (resource id / reference / title,
intent, confirmation) pass through, and the output gate scans every nested
string of the output recursively as a second line of defence.

Error containment: on EVERY error return the node ships the contained
envelope built by `_error_output()` (the error status + a constant reason
code, never resource evidence) AND blanks the evidence fields listed in
`_EVIDENCE_STATE_FIELDS`. Both halves are load-bearing:

  - The envelope must stay a NON-EMPTY mapping. AgentBaseGraph.get_output()
    projects `formatted_output or result` with no status check, so an absent
    or falsy formatted_output re-opens that fallback onto whatever survived
    in state - which is how the merged inner output used to reach a caller
    who was being told the action FAILED.
  - Omitting the identifiers from one envelope is containment in form only.
    resource_id / resource_ref ARE the GitLab write evidence (the gate below
    REFUSES a SUCCESS that lacks them), so they are cleared from state too -
    otherwise a checkpoint or a downstream reader picks them straight back up.

The envelope is a CLOSED SET. The caller-visible error carries only values
this module chose: the error status and one of the reason codes in
`ERROR_REASONS`. Nothing read from `error_log`, from the gate's violation
entries, or from any other node-authored string is projected into it. Those
lines are free text - an API-call failure puts an upstream response body
there, and identifiers, names and emails are one f-string away - so
truncating or redacting them is not a bounded contract; not publishing them
is. `error_log` itself is untouched: it is the internal channel the state
reducer appends to and the audit trail reads. It is simply not the caller's.
"""

import re
from typing import Any, Iterator

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

# Credential-shaped strings that must never reach the caller.
#
# The scan is the UNION of two sets - never one INSTEAD of the other:
#
#   * the LOCAL patterns below carry shapes the platform detector does not
#     know. `glpat-` is the one that matters here: a GitLab personal access
#     token is the credential this agent's own domain makes likely, and
#     detect_credentials() does not recognise it.
#   * detect_credentials() is CALLED, not copied, so this gate can never end up
#     narrower than the scan the framework runs over the same delta moments
#     later, and cannot drift narrower when the platform adds a pattern.
#
# A narrower local set is a bypass, not a lighter second line of defence: a
# value this gate misses and the framework catches makes the @final
# FunctionNode._security_gate_output RAISE, and BaseNode.__call__ then discards
# the node's whole delta - the clearing in _contained_error_delta() with it.
_CREDENTIAL_LIKE_RE = re.compile(
    r"eyJ[A-Za-z0-9._-]{10,}|sk-[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]{16,}"
)


def _is_credential_like(text: str) -> bool:
    """True when the text carries a credential shape - local patterns OR the platform's."""
    return bool(_CREDENTIAL_LIKE_RE.search(text)) or bool(detect_credentials(text))


# State fields that carry GitLab ACTION EVIDENCE or record content. Every error
# return blanks all of them (see the module docstring): resource_id /
# resource_ref are the proof a write happened and which record it touched,
# resource_title / confirmation / result / gitlab_payload are that record's
# content. `result` is blanked to None rather than "" because it is the field
# the framework's `formatted_output or result` projection falls back to.
#
# This tuple is hand-maintained, so a field added to
# GitLabWorkflowGraphNode.merge_output() later could silently miss it.
# tests/unit/test_post_process_node.py::TestClearedSetInventory pins the
# correspondence: every merged key must be cleared here, be a control channel
# (status / error_log), or be declared inert there on purpose.
_EVIDENCE_STATE_FIELDS = (
    "resource_id",
    "resource_ref",
    "resource_title",
    "confirmation",
    "gitlab_payload",
)

# Reason codes - the ONLY values, besides the error status, that the
# caller-visible ERROR envelope may carry. Chosen here, never derived from
# state, so the envelope is a closed set.
_REASON_WORKFLOW_FAILED = "workflow_failed"  # the inner workflow reported an error
_REASON_OUTPUT_WITHHELD = "output_withheld"  # the output gate refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})


def _error_output(reason: str) -> "dict[str, str]":
    """Caller-facing error envelope: the error status and a constant reason code.

    Deliberately never empty - a falsy formatted_output reactivates
    AgentBaseGraph.get_output()'s `formatted_output or result` fallback.
    """
    return {"status": AgentStatus.ERROR.value, "reason": reason}


def _contained_error_delta(reason: str) -> "dict[str, Any]":
    """The full error delta: contained envelope + every evidence field cleared.

    `reason` is one of ERROR_REASONS. Nothing from error_log enters the
    envelope: the inner entries already sit in state (operator.add reducer),
    and a gate violation appends its own entries at the call site - both stay
    on the internal channel.
    """
    delta: dict[str, Any] = {field: "" for field in _EVIDENCE_STATE_FIELDS}
    delta["result"] = None
    delta["formatted_output"] = _error_output(reason)
    delta["status"] = AgentStatus.ERROR.value
    return delta


def _iter_nested_strings(value: object) -> "Iterator[str]":
    """Yield every string reachable inside value (dict/list/tuple recursion)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _iter_nested_strings(key)
            yield from _iter_nested_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_nested_strings(child)


def _security_gate_output(formatted_output: "dict[str, Any]", is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()
    and from GitLabIssueMRAgent.get_output()).

    Blocks (returns violations for):
      - a SUCCESS response with no resource evidence (resource_id/resource_ref),
        which would misrepresent the GitLab action outcome to the caller;
      - any credential-shaped string in the caller-facing output - scanned
        RECURSIVELY: nested dict/list values (and keys) are inspected too, so
        a surfaced container can never smuggle a credential past the gate.

    Violations are error_log entries (internal); they never reach the caller.
    """
    problems: list[str] = []
    if is_success and not (formatted_output.get("resource_id") or formatted_output.get("resource_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing resource_id/resource_ref evidence")
    for key, value in formatted_output.items():
        for text in _iter_nested_strings(value):
            if _is_credential_like(text):
                problems.append(f"PostProcess output gate: credential-like value in formatted_output['{key}']")
                break
    return problems


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Trust: read-only formatting of the already-produced result - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        errored = state.get("status") == AgentStatus.ERROR.value

        # If the inner workflow errored, preserve the error status (do not mask
        # it) - and do not re-publish the action evidence while doing so. The
        # previous shape blanked `confirmation` but rebuilt resource_id /
        # resource_ref out of the very state it was containing, so an error
        # envelope disclosed that a GitLab write had happened and which record
        # it touched. error_log is NOT re-emitted here (it already carries an
        # operator.add reducer, so the entries stay in state untouched) and it
        # is NOT projected either: the caller receives the reason code only.
        if errored:
            return _contained_error_delta(_REASON_WORKFLOW_FAILED)

        # Scalar evidence fields only - the raw request payload dict is never
        # echoed back to the caller (its accepted content is represented by
        # the confirmation + evidence fields, not by a wholesale dump).
        formatted_output = {
            "resource_id": state.get("resource_id", ""),
            "resource_ref": state.get("resource_ref", ""),
            "resource_title": state.get("resource_title", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
        }

        # Domain output gate (module-level helper - see module docstring).
        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            # Refusing to publish `formatted_output` is not containment on its
            # own: with no formatted_output written, get_output() falls back to
            # `result` - the merged inner output (id + ref + confirmation) -
            # and ships the same content the gate just blocked. So emit the
            # contained envelope and clear the evidence from state as well.
            # The violation entries go to error_log - the internal channel -
            # and never into the envelope.
            blocked = _contained_error_delta(_REASON_OUTPUT_WITHHELD)
            blocked["error_log"] = violations
            return blocked

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_resource_id": bool(state.get("resource_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
