# CMN-C2-239 - Unit tests: ConfirmNode (inner Step 5).
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "resource_id": "4321",
        "resource_ref": "gitlab://example-group/webapp/issues/4321",
        "resource_title": "login fails on safari",
        "intent": "create_issue",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_issue_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Created GitLab issue" in result["confirmation"]
        assert "login fails on safari" in result["confirmation"]
        assert "ref=gitlab://example-group/webapp/issues/4321" in result["confirmation"]
        assert "id=4321" in result["confirmation"]
        assert result["result"]["resource_id"] == "4321"
        assert result["result"]["resource_ref"] == "gitlab://example-group/webapp/issues/4321"

    def test_merge_request_verb(self):
        result = self.node(
            _state(
                intent="open_merge_request",
                resource_ref="gitlab://example-group/webapp/merge_requests/7",
                resource_title="login hotfix",
            )
        )
        assert "Opened merge request" in result["confirmation"]

    def test_comment_verb(self):
        result = self.node(
            _state(
                intent="post_comment",
                resource_ref="gitlab://example-group/webapp/issues/12#note_88",
                resource_title="#12",
            )
        )
        assert "Posted comment on" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed GitLab request" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(resource_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=4321" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_resource_id_when_title_missing(self):
        result = self.node(_state(resource_title=""))
        assert "'4321'" in result["confirmation"]

    def test_missing_resource_evidence_errors(self):
        result = self.node(_state(resource_id="", resource_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_reference_presence_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.confirm_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        assert payloads["confirm_complete"] == {"intent": "create_issue", "has_resource_ref": True}
