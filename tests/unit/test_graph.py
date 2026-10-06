# CMN-C2-239 - Unit tests: outer GitLabIssueMRAgent (AgentBaseGraph) contract.
# This template is a structured-product agent, so get_output() EXTENDS the
# base envelope with the structured action keys (SUCCESS-only, re-scanned by
# the output gate, fail-closed) - that override is unit-checked here. The
# compiled end-to-end path is PB-6.

import json

from framework.schemas.agent_status import AgentStatus

from src.graph.graph import GitLabIssueMRAgent, GitLabWorkflowGraphNode
from src.nodes.post_process_node import _REASON_OUTPUT_WITHHELD
from src.schemas.state import State


def _success_state(**overrides) -> dict:
    state = {
        "user_input": "Create an issue titled 'login fails on safari' in project example-group/webapp.",
        "status": AgentStatus.SUCCESS.value,
        "intent": "create_issue",
        "project_id": "example-group/webapp",
        "resource_id": "4321",
        "resource_ref": "gitlab://example-group/webapp/issues/4321",
        "resource_title": "login fails on safari",
        "confirmation": "Created GitLab issue - id=4321",
        "formatted_output": {"resource_id": "4321", "confirmation": "Created GitLab issue - id=4321"},
        "result": {"resource_id": "4321"},
        "error_log": [],
        "node_history": [
            "InitializeNode",
            "PreProcessNode",
            "GitLabWorkflowGraphNode",
            "PostProcessNode",
            "FinalizeNode",
        ],
        "trace_id": "tr",
        "correlation_id": "co",
        "execution_time": {},
    }
    state.update(overrides)
    return state


def test_outer_agent_identity():
    agent = GitLabIssueMRAgent()
    assert agent.name == "cmn_c2_239"
    assert agent.state_schema is State


def test_parent_config_forwards_runtime_gitlab_section():
    # The known Cat-2 defect is an empty _parent_config() that makes every
    # declared setting dead - assert the gitlab section of config/config.yaml
    # actually reaches config["configurable"]. The backbone keys (max_retry,
    # timeout_s) travel through the agent CONSTRUCTOR instead (no inner node
    # reads them) - test_config.py proves that arrival.
    node = GitLabWorkflowGraphNode()
    configurable = node._parent_config()["configurable"]
    assert configurable["gitlab"]["base_url"] == "https://gitlab.com/api/v4"
    assert "agent" not in configurable


def test_graph_node_does_not_propagate_hitl():
    # Non-HITL template: no cross-boundary interrupt propagation (PB-7 stub).
    assert GitLabWorkflowGraphNode.propagate_hitl is False
    # Inner rejections are normal domain outcomes: contained as a structured
    # error envelope (never re-raised into a traceback-recording handler).
    assert GitLabWorkflowGraphNode.error_strategy == "handle"


def test_get_output_success_extends_envelope_with_structured_keys():
    agent = GitLabIssueMRAgent()
    out = agent.get_output(_success_state())
    # Structured action keys surface at the envelope top level on SUCCESS.
    assert out["intent"] == "create_issue"
    assert out["project_id"] == "example-group/webapp"
    assert out["resource_id"] == "4321"
    assert out["resource_ref"] == "gitlab://example-group/webapp/issues/4321"
    assert out["confirmation"] == "Created GitLab issue - id=4321"


def test_get_output_error_returns_base_envelope_only():
    agent = GitLabIssueMRAgent()
    out = agent.get_output(
        _success_state(
            status=AgentStatus.ERROR.value,
            error_log=["ClassifyIntentNode: could not classify the request"],
        )
    )
    # Error path: base envelope only - no structured action keys.
    assert "resource_id" not in out
    assert "resource_ref" not in out
    assert "project_id" not in out


def test_get_output_fails_closed_on_credential_shaped_structured_value():
    # Output-gate re-scan of the structured surface: a violation suppresses
    # the structured keys and fails the response closed (status=error) with a
    # closed-set reason code - never the violation text or error_log.
    bearer_like = "Bearer " + "a" * 24
    agent = GitLabIssueMRAgent()
    out = agent.get_output(_success_state(confirmation=bearer_like))
    assert out["status"] == AgentStatus.ERROR.value
    assert out["output"] is None
    assert out["reason"] == _REASON_OUTPUT_WITHHELD
    assert "error_log" not in out
    assert "resource_id" not in out
    assert bearer_like not in json.dumps(out, default=str)


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. The token is
    assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


def test_get_output_re_scan_refusal_projects_no_error_log_text():
    # A credential-shaped project_id is the one surface only the re-scan sees
    # (post_process does not publish project_id). That refusal used to copy the
    # run's error_log - which can carry upstream response text - into the
    # envelope. The caller now receives the reason code only.
    agent = GitLabIssueMRAgent()
    out = agent.get_output(_success_state(project_id="Bearer " + "a" * 24, error_log=[_sentinel()]))
    rendered = json.dumps(out, default=str)
    assert out["status"] == AgentStatus.ERROR.value
    assert out["output"] is None
    assert out["reason"] == _REASON_OUTPUT_WITHHELD
    assert "error_log" not in out
    assert "A. Tanaka" not in rendered
    assert "boom: upstream" not in rendered
    assert "output gate" not in rendered


def test_get_output_success_envelope_carries_no_reason_code():
    out = GitLabIssueMRAgent().get_output(_success_state())
    assert out["status"] == AgentStatus.SUCCESS.value
    assert "reason" not in out
