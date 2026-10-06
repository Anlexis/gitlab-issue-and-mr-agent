# CMN-C2-239 - Unit tests: ClassifyIntentNode (inner Step 2).
# Intents: create_issue / open_merge_request / post_comment (deterministic
# keyword heuristic, v1 - no LLM). Every intent of this agent is a WRITE, so
# a no-signal request FAILS CLOSED (status=error) - there is no read-only
# fallback and a write is never guessed.
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_create_issue(self):
        result = self.node(_state("Create an issue titled 'login fails on safari' in project example-group/webapp."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "create_issue"

    def test_keyword_open_merge_request(self):
        result = self.node(_state("Open a merge request from feature/login-fix into develop titled 'login hotfix'"))
        assert result["intent"] == "open_merge_request"

    def test_keyword_post_comment(self):
        result = self.node(_state("Comment on #12 in project example-group/webapp with the pipeline result"))
        assert result["intent"] == "post_comment"

    def test_comment_wins_over_merge_request_mention(self):
        # Priority order is most-specific-first: "comment on merge request !5"
        # classifies as the comment, never the merge request.
        result = self.node(
            _state("Comment on merge request !5 in project example-group/webapp with the review verdict")
        )
        assert result["intent"] == "post_comment"

    def test_merge_request_wins_over_issue_reference(self):
        result = self.node(_state("Open a merge request for #12 from feature/fix-12 into develop"))
        assert result["intent"] == "open_merge_request"

    def test_japanese_keyword_create_issue(self):
        result = self.node(_state("プロジェクト example-group/webapp に課題を起票してください"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "create_issue"

    def test_explicit_contract_intent_wins(self):
        # A validated caller-contract intent never depends on keyword inference.
        state = _state("handle the login problem")
        state["gitlab_request"] = '{"intent":"open_merge_request"}'
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "open_merge_request"

    def test_invalid_contract_intent_falls_back_to_keywords(self):
        # Defence in depth: only the validated enum is honoured - anything
        # else falls back to (fail-closed) keyword classification.
        state = _state("create an issue for the login problem")
        state["gitlab_request"] = '{"intent":"delete_everything"}'
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "create_issue"

    def test_no_signal_fails_closed(self):
        # Every intent of this agent is a WRITE - low-confidence classification
        # must never guess a write (docs/02 Design Decision Record).
        result = self.node(_state("please take care of this for the team"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("could not classify" in entry for entry in result["error_log"])
        assert "intent" not in result

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Create an issue titled 'login fails on safari' in project example-group/webapp."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"] == {"intent": "create_issue"}
