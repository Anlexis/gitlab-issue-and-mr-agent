# CMN-C2-239 - Unit tests: inner GitLabWorkflowGraph (BaseGraph) contract.
# The compiled outer path is
# exercised end-to-end by tests/proof_of_boundary/test_pb_invoke_order.py; this
# module unit-checks the inner graph's identity, config forwarding, routing,
# output contract, and a direct inner invoke on the network-free v1 stub.

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import GitLabWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return GitLabWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "gitlab_issue_mr_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_gitlab_config_as_json():
    g = _graph({"configurable": {"gitlab": {"base_url": "https://gitlab.example.test/api/v4"}}})
    extra = g._extra_initial_state()
    # State contract: forwarded as a JSON string, not a native dict.
    assert isinstance(extra["gitlab_config"], str)
    assert from_json(extra["gitlab_config"], {}) == {"base_url": "https://gitlab.example.test/api/v4"}


def test_extra_initial_state_empty_without_gitlab_section():
    assert _graph()._extra_initial_state() == {}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_resource_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {
                "resource_id": "4321",
                "resource_ref": "gitlab://example-group/webapp/issues/4321",
                "confirmation": "ok",
            },
            "status": AgentStatus.SUCCESS.value,
            "intent": "create_issue",
            "project_id": "example-group/webapp",
            "resource_id": "4321",
            "resource_ref": "gitlab://example-group/webapp/issues/4321",
            "resource_title": "login fails on safari",
            "confirmation": "ok",
            "gitlab_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "create_issue"
    assert out["resource_ref"] == "gitlab://example-group/webapp/issues/4321"
    assert out["confirmation"] == "ok"
    assert out["output"] == {
        "resource_id": "4321",
        "resource_ref": "gitlab://example-group/webapp/issues/4321",
        "confirmation": "ok",
    }


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_create_issue_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"gitlab": {"base_url": "https://gitlab.com/api/v4"}}})
    g.compile()
    result = g.invoke(
        user_input='Create an issue titled "stub check" in project example-group/webapp with description: created by the unit suite'
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["intent"] == "create_issue"
    assert result["resource_id"]
    assert result["resource_ref"].startswith("gitlab://example-group/webapp/issues/")
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferGitLabFieldsNode",
        "CallGitLabApiNode",
        "ConfirmNode",
    ]
