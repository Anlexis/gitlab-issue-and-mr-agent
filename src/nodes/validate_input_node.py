"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-request input and runs a deterministic (regex, NOT LLM)
scan of the inbound text for email addresses / access-token-like strings,
which are flag-and-redacted before anything is logged. A GitLab write request
legitimately names projects, branches, and issue numbers (the framework PII
mask in BaseNode.__call__ additionally masks emails/phones/names in
user_input / validated_input), so this is flag-and-redact for safe logging,
not a hard reject. The only deterministic auto-reject is the empty /
non-request guard - the instruction-override screen already ran, fail closed,
in the outer pre_process (src/services/security.py).
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import redact_sensitive

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3


class ValidateInputNode(FunctionNode):
    """Validate + flag-and-redact the inbound GitLab write request."""

    # Trust: inner domain node - the external gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        text = raw
        project_hint = state.get("project_hint", "")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                project_hint = obj.get("project_hint", project_hint)
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Deterministic flag-and-redact (before any logging). Local values per
        # invocation - never a module-global (no cross-invoke leak).
        redacted, flags = redact_sensitive(text)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_project_hint": bool(project_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "project_hint": project_hint,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
