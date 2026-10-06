# CMN-C2-239 - Trust-gate boundary tests (unit).
#
# Nodes are invoked via node(state) - BaseNode.__call__ routes the full
# security pipeline (trust gate -> input gate -> execute() -> output gate) -
# NOT via node.execute(state), which would bypass the gate. The rejection
# test asserts on the RETURNED error dict (__call__ never raises for a
# trust denial).

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.pre_process_node import PreProcessNode


def _base_state(**overrides) -> dict:
    state = {
        "user_input": "Create an issue titled 'login fails on safari' in project example-group/webapp.",
        "input_context": {},
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "s1-gate-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestTrustGate:
    """PreProcessNode is the single external VERIFIED_EXTERNAL gate."""

    def test_anonymous_caller_is_denied(self):
        node = PreProcessNode()
        state = _base_state(caller_trust_level=TrustLevel.ANONYMOUS.value)
        result = node(state)  # __call__ RETURNS an error dict - never raises
        assert result["status"] == AgentStatus.ERROR.value
        assert any("trust gate denied" in entry for entry in result["error_log"])
        # Denied BEFORE execute() ran: execute-only keys are absent.
        assert "validated_input" not in result
        assert "project_hint" not in result

    def test_verified_external_caller_passes_gate(self):
        node = PreProcessNode()
        result = node(_base_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = json.loads(result["validated_input"])
        assert "example-group/webapp" in payload["text"]

    def test_inner_nodes_accept_anonymous_caller(self):
        """Inner domain nodes declare ANONYMOUS - the gate lives on pre_process only."""
        from src.nodes.classify_intent_node import ClassifyIntentNode

        node = ClassifyIntentNode()
        state = _base_state(
            caller_trust_level=TrustLevel.ANONYMOUS.value,
            validated_input="Create an issue titled 'login fails on safari' in project example-group/webapp.",
        )
        result = node(state)  # passes the trust gate (ANONYMOUS >= ANONYMOUS)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "create_issue"

    def test_trust_posture_declarations(self):
        """The single external gate is pre_process; every inner domain node
        (incl. the GitLab API call, which runs under the caller's UNELEVATED
        context) declares ANONYMOUS."""
        from src.nodes.call_gitlab_api_node import CallGitLabApiNode
        from src.nodes.classify_intent_node import ClassifyIntentNode
        from src.nodes.confirm_node import ConfirmNode
        from src.nodes.infer_gitlab_fields_node import InferGitLabFieldsNode
        from src.nodes.post_process_node import PostProcessNode
        from src.nodes.validate_input_node import ValidateInputNode

        assert PreProcessNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL
        for node_cls in (
            ValidateInputNode,
            ClassifyIntentNode,
            InferGitLabFieldsNode,
            CallGitLabApiNode,
            ConfirmNode,
            PostProcessNode,
        ):
            assert node_cls.required_trust_level == TrustLevel.ANONYMOUS, node_cls.__name__
