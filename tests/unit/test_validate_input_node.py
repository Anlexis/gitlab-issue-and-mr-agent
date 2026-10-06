# CMN-C2-239 - Unit tests: ValidateInputNode (inner Step 1, flag-and-redact).
#
# Canon: nodes are invoked via node(state) - through BaseNode.__call__, which
# routes the full security pipeline - never bare node.execute(state). This
# inner domain node declares ANONYMOUS, so the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value.
#
# Redaction layering exercised here:
#   * the FRAMEWORK mask in __call__ rewrites emails (any '@') in
#     validated_input to "[MASKED]" BEFORE execute() sees the text - the
#     intentional-PII test asserts that [MASKED] path;
#   * the NODE's own deterministic scan handles token-shaped strings the
#     framework mask does not cover (secret_* / sk-* / eyJ* / glpat-*) -
#     flag + [REDACTED].

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.validate_input_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "validated_input": "Create an issue titled 'login fails on safari' in project example-group/webapp.",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "validate-input-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestValidateInputNode:
    def setup_method(self):
        self.node = ValidateInputNode()

    def test_success_plain_text(self):
        result = self.node(_state(validated_input="open an issue for the failing login flow"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "open an issue for the failing login flow"
        assert from_json(result["redaction_flags"], None) == []

    def test_success_serialized_json_input(self):
        payload = json.dumps(
            {"text": "comment on #12 with the pipeline result", "project_hint": "example-group/webapp"}
        )
        result = self.node(_state(validated_input=payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "comment on #12 with the pipeline result"
        assert result["project_hint"] == "example-group/webapp"

    def test_empty_input_errors(self):
        result = self.node(_state(validated_input="  "))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_short_input_errors(self):
        result = self.node(_state(validated_input="ab"))
        assert result["status"] == AgentStatus.ERROR.value

    def test_framework_masks_email_before_execute(self):
        """Intentional-PII path: the framework mask in __call__ rewrites the
        email to [MASKED] before execute() runs, so no raw address survives."""
        result = self.node(
            _state(validated_input="assign the new issue in project example-group/webapp to dev.lead@example.com")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "dev.lead@example.com" not in result["validated_input"]
        assert "[MASKED]" in result["validated_input"]

    def test_node_redacts_token_shaped_string(self):
        """The node's own deterministic scan covers token shapes the framework
        PII mask does not (secret_*): flagged + [REDACTED] before logging."""
        text = "register integration key secret_abcdef123456 in project example-group/webapp"
        result = self.node(_state(validated_input=text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "secret_abcdef123456" not in result["validated_input"]
        assert "[REDACTED]" in result["validated_input"]
        assert "token" in from_json(result["redaction_flags"], [])

    def test_node_redacts_gitlab_pat_shape(self):
        """glpat-* (a pasted GitLab PAT) is flagged + redacted the same way."""
        # Fixture built at runtime so no token-shaped literal is committed.
        pat_like = "glpat-" + "abcdef123456"
        text = f"rotate the ci token {pat_like} for project example-group/webapp"
        result = self.node(_state(validated_input=text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert pat_like not in result["validated_input"]
        assert "[REDACTED]" in result["validated_input"]
        assert "token" in from_json(result["redaction_flags"], [])

    def test_audit_emits_scan_outcome_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.validate_input_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state(validated_input="open an issue for the failing login flow"))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - flags only, never the text.
        assert payloads["validate_input_complete"]["redaction_flags"] == []
        assert "text" not in payloads["validate_input_complete"]
