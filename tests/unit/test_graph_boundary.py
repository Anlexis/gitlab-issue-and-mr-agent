# CMN-C2-239 - Outer/inner graph boundary behaviour.
#
# Three contracts proven here:
#   1. Error containment: an inner-graph rejection (a caller mistake, not a
#      bug) surfaces as a structured error envelope with the inner node's own
#      field-naming message - never an exception, never a Python traceback
#      (runtime paths) in error_log.
#   2. The context bridge: the validated structured caller contract crosses
#      the graph-node boundary (which does not forward input_context) and a
#      Title-Case title arrives at the GitLab API call VERBATIM - the platform
#      mask rewrites Title-Case runs on the free-text path, which is exactly
#      why the structured channel exists. Both directions are pinned: the
#      bridge preserves, the text path masks (the control that proves the
#      verifier can tell the difference).
#   3. Numeric fidelity: rendered identifiers pass through byte-identical -
#      this template renders no monetary aggregates, so there is no rounding
#      grid, and nothing may rewrite a number anywhere in the output.

import json

from framework.errors import SubgraphError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import GitLabIssueMRAgent, GitLabWorkflowGraphNode, load_runtime_config
from src.services.gitlab_client import GitLabClient


def _agent():
    agent = GitLabIssueMRAgent(config=load_runtime_config())
    agent.compile()
    return agent


def _ctx(caller_id="boundary-test"):
    return InvocationContext(caller_id=caller_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


def _node_state(**overrides):
    state = {
        "user_input": "Create an issue in project example-group/webapp about the login timeout",
        "validated_input": json.dumps({"text": "Create an issue about the login timeout", "project_hint": ""}),
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "session_id": "boundary-test",
        "correlation_id": "boundary-test",
        "thread_id": "boundary-test",
        "caller_id": "boundary-test",
        "trace_id": "boundary-test",
        "hitl_allowed": True,
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _RejectingSubgraph:
    name = "gitlab_issue_mr_workflow"

    def invoke(self, user_input, session_id="", ctx=None):
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": ["ClassifyIntentNode: could not classify the request"],
        }


class _CrashingSubgraph:
    name = "gitlab_issue_mr_workflow"

    def invoke(self, user_input, session_id="", ctx=None):
        raise RuntimeError("boom at /private/lib/runtime/path.py line 42")


class TestErrorContainment:
    def test_inner_rejection_returns_the_domain_message_not_a_traceback(self, monkeypatch):
        node = GitLabWorkflowGraphNode()
        monkeypatch.setattr(node, "get_subgraph", lambda: _RejectingSubgraph())
        result = node.execute(_node_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"] == ["ClassifyIntentNode: could not classify the request"]
        assert "Traceback" not in " ".join(result["error_log"])

    def test_unexpected_inner_exception_is_contained_and_generic(self, monkeypatch):
        # Exception text can carry runtime paths - the caller gets a generic
        # message, never the exception's own wording.
        node = GitLabWorkflowGraphNode()
        monkeypatch.setattr(node, "get_subgraph", lambda: _CrashingSubgraph())
        result = node.execute(_node_state())
        assert result["status"] == AgentStatus.ERROR.value
        joined = " ".join(result["error_log"])
        assert "/private" not in joined
        assert "boom" not in joined
        assert "Traceback" not in joined

    def test_on_subgraph_error_unwraps_subgraph_error_log(self):
        node = GitLabWorkflowGraphNode()
        err = SubgraphError(agent_name="inner", error_log=["InnerNode: field 'x' is invalid"], trace_id="t")
        result = node.on_subgraph_error({}, err)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"] == ["InnerNode: field 'x' is invalid"]

    def test_inner_rejection_through_the_compiled_graph_is_an_envelope_not_a_crash(self):
        agent = _agent()
        result = agent.invoke(
            user_input="please summarise the weather in the alps",
            session_id="boundary-reject",
            ctx=_ctx(),
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert not result.get("output")
        # Fail-closed envelope: no resource keys on a non-SUCCESS status.
        assert "resource_id" not in result
        assert "resource_ref" not in result

    def test_no_traceback_reaches_the_final_state_error_log(self):
        # The compiled end-to-end path: an ordinary caller mistake must leave
        # only domain messages behind - a traceback in error_log means the
        # containment strategy regressed to re-raising.
        agent = _agent()
        initial = _node_state(user_input="please summarise the weather in the alps")
        initial.update({"schema_version": 1, "status": "pending", "retry_count": 0, "hitl_count": 0})
        del initial["validated_input"]
        final_state = agent._compiled.invoke(initial)
        joined = "\n".join(final_state.get("error_log", []))
        assert "Traceback" not in joined
        assert ".py" not in joined


class TestContextBridgeEndToEnd:
    def _capture_issue_payload(self, monkeypatch):
        captured = {}
        original = GitLabClient.create_issue

        def spy(self, project, payload, api_token):
            captured["project"] = project
            captured["payload"] = payload
            return original(self, project, payload, api_token)

        monkeypatch.setattr(GitLabClient, "create_issue", spy)
        return captured

    def test_title_case_title_survives_verbatim_via_the_structured_channel(self, monkeypatch):
        captured = self._capture_issue_payload(monkeypatch)
        agent = _agent()
        result = agent.invoke(
            user_input="Create an issue about the login timeout",
            session_id="bridge-pin",
            ctx=_ctx(),
            input_context={
                "project": "example-group/webapp",
                "intent": "create_issue",
                "title": "Fix Login Timeout Bug",
                "labels": ["bug"],
            },
        )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        # The pin: byte-identical arrival at the API boundary.
        assert captured["payload"]["title"] == "Fix Login Timeout Bug"
        assert captured["payload"]["labels"] == "bug"
        assert captured["project"] == "example-group/webapp"
        assert result["resource_title"] == "Fix Login Timeout Bug"

    def test_control_the_free_text_path_is_masked_by_the_platform(self, monkeypatch):
        # The control that proves the pin above is meaningful: the SAME title
        # carried in free text is rewritten by the platform's Title-Case mask
        # before extraction - so an agent relying on text extraction would
        # create an issue titled "[MASKED]". If this control ever starts
        # passing the title through verbatim, the platform behaviour changed
        # and the channel guidance in docs/02 must be revisited.
        captured = self._capture_issue_payload(monkeypatch)
        agent = _agent()
        result = agent.invoke(
            user_input='Create an issue titled "Fix Login Timeout Bug" in project example-group/webapp',
            session_id="bridge-control",
            ctx=_ctx(),
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert captured["payload"]["title"] == "[MASKED]"

    def test_explicit_intent_from_the_contract_wins(self, monkeypatch):
        captured = self._capture_issue_payload(monkeypatch)
        agent = _agent()
        # Text alone would be low-confidence ("handle the login timeout") -
        # the validated explicit intent makes it a create_issue.
        result = agent.invoke(
            user_input="handle the login timeout in the auth service",
            session_id="bridge-intent",
            ctx=_ctx(),
            input_context={
                "project": "example-group/webapp",
                "intent": "create_issue",
                "title": "login timeout",
            },
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "create_issue"
        assert captured["payload"]["title"] == "login timeout"

    def test_comment_target_fields_cross_the_bridge(self, monkeypatch):
        captured = {}
        original = GitLabClient.post_comment

        def spy(self, project, target_kind, target_iid, payload, api_token):
            captured.update(project=project, kind=target_kind, iid=target_iid, payload=payload)
            return original(self, project, target_kind, target_iid, payload, api_token)

        monkeypatch.setattr(GitLabClient, "post_comment", spy)
        agent = _agent()
        result = agent.invoke(
            user_input="add a note about the deployment result",
            session_id="bridge-comment",
            ctx=_ctx(),
            input_context={
                "project": "example-group/webapp",
                "intent": "post_comment",
                "target_kind": "merge_request",
                "target_iid": 45,
                "comment": "Deployment verified on the staging cluster",
            },
        )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        assert captured["kind"] == "merge_request"
        assert captured["iid"] == "45"
        assert captured["payload"]["body"] == "Deployment verified on the staging cluster"


class TestNumericFidelity:
    def test_rendered_identifiers_are_byte_identical(self):
        # No rounding grid applies (no monetary aggregates are rendered):
        # a pure-numeric identifier must pass through untouched - never
        # grouped, rounded, or rewritten.
        agent = _agent()
        result = agent.invoke(
            user_input="comment on the retry budget",
            session_id="fidelity-pin",
            ctx=_ctx(),
            input_context={
                "project": "example-group/sku_48210",
                "intent": "post_comment",
                "target_kind": "issue",
                "target_iid": 12345,
                "comment": "retry budget exhausted after the 90d horizon",
            },
        )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        confirmation = result["confirmation"]
        assert "example-group/sku_48210/issues/12345" in result["resource_ref"]
        assert "12,345" not in confirmation and "12,000" not in confirmation
        assert "#12345" in confirmation
