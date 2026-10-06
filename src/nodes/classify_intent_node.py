"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the request into one of create_issue / open_merge_request /
post_comment. An explicit, already-validated `intent` supplied through the
structured caller contract (`gitlab_request`) wins outright; otherwise a
deterministic keyword heuristic classifies the (redacted) request text, so
the template is testable and runnable without a live LLM (docs/02
"v1 Implementation Note"). Every intent of this agent is a WRITE, so there is
no read-only fallback: a no-signal / low-confidence request FAILS CLOSED
(status=error, no GitLab call is ever made on a guess).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

_VALID_INTENTS = ("create_issue", "open_merge_request", "post_comment")

# Deterministic keyword signals (checked in priority order: the most specific
# operation first, so "comment on merge request !5" classifies as the comment
# and "open a merge request for #12" classifies as the merge request).
_KEYWORDS = (
    ("post_comment", ("comment", "reply to", "reply on", "add a note", "note on", "コメント", "返信")),
    (
        "open_merge_request",
        (
            "merge request",
            "merge-request",
            "open an mr",
            "open a mr",
            "open mr",
            "mr from",
            "raise an mr",
            "create an mr",
            "pull request",
            "マージリクエスト",
            "マージ依頼",
        ),
    ),
    ("create_issue", ("issue", "ticket", "bug report", "file a bug", "起票", "チケット", "課題")),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a GitLab write-operation intent."""

    # Trust: inner domain node, read-only classification of already-redacted
    # text - the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        # Explicit intent from the validated caller contract wins: it was
        # checked against the supported-write enum upstream, so a caller who
        # states the operation never depends on keyword inference.
        request = dict(from_json(state.get("gitlab_request"), {}) or {})
        explicit = str(request.get("intent", "") or "")
        intent = explicit if explicit in _VALID_INTENTS else self._classify_via_keywords(text)

        if intent not in _VALID_INTENTS:
            # Fail closed: every intent is a write - never guess one
            # (docs/02_design.md Design Decision Record "Unknown intent").
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "ClassifyIntentNode: could not classify the request into "
                    "create_issue / open_merge_request / post_comment - "
                    "no write action executed"
                ],
            }

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent},
            state,
        )

        return {"intent": intent, "status": AgentStatus.SUCCESS.value}

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: sentinel outside _VALID_INTENTS -> the fail-closed
        # guard in execute() returns status=error (never a guessed write).
        return "unknown"
