"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the created issue / opened merge request / posted comment (id +
reference + title) into a human-readable confirmation message, surfacing the
affected resource for human review (risk mitigation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VERBS = {
    "create_issue": "Created GitLab issue",
    "open_merge_request": "Opened merge request",
    "post_comment": "Posted comment on",
}


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # Trust: inner domain node, read-only formatting of already-produced data -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        resource_id = state.get("resource_id", "")
        resource_ref = state.get("resource_ref", "")
        resource_title = state.get("resource_title", "")
        intent = state.get("intent", "")

        if not resource_id and not resource_ref:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no resource_id/resource_ref to confirm"],
            }

        verb = _VERBS.get(intent, "Processed GitLab request")
        parts = [f"{verb} '{resource_title or resource_id}'"]
        if resource_ref:
            parts.append(f"ref={resource_ref}")
        if resource_id:
            parts.append(f"id={resource_id}")
        confirmation = " - ".join(parts)

        # Audit the confirmed action - intent + reference presence (no content).
        emit_trace_event(
            "confirm_complete",
            {"intent": intent, "has_resource_ref": bool(resource_ref)},
            state,
        )

        return {
            "confirmation": confirmation,
            "result": {
                "resource_id": resource_id,
                "resource_ref": resource_ref,
                "confirmation": confirmation,
            },
            "status": AgentStatus.SUCCESS.value,
        }
