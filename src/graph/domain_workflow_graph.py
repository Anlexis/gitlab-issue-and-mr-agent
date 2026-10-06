"""AgentCore Platform v1.0 - inner GitLab workflow graph (Cat 2 domain workflow).

Instantiated by GitLabWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_gitlab_fields
          -> call_gitlab_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    gitlab   - runtime integration section from config/config.yaml (base_url);
               injected into State as the JSON `gitlab_config` field via
               _extra_initial_state() so the no-arg nodes can read it

The validated structured caller fields (title, labels, branches, ...) arrive
through the context bridge (src/graph/context_bridge.py) - the graph-node
boundary does not forward them - and are injected into State as the JSON
`gitlab_request` field alongside gitlab_config.

Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_gitlab_request
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_gitlab_fields_node import InferGitLabFieldsNode
from src.nodes.call_gitlab_api_node import CallGitLabApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State, to_json


class GitLabWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "gitlab_issue_mr_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the gitlab section is optional (the client
        # falls back to the documented default base_url + the network-free
        # stub transport), and a missing/unusable setting is handled at
        # CallGitLabApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_gitlab_fields"] = InferGitLabFieldsNode()
        self._nodes["call_gitlab_api"] = CallGitLabApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_gitlab_fields")
        self._sg.add_edge("infer_gitlab_fields", "call_gitlab_api")
        self._sg.add_edge("call_gitlab_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        # Required by BaseGraph ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it. Annotated with this graph's
        # OWN State on purpose: the graph runtime reads a path callable's
        # annotation as its input schema and projects other fields away, so a
        # base-schema annotation would silently hide the domain fields if a
        # conditional edge is ever added.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> "dict[str, Any]":
        # Forward the runtime `gitlab` section (arriving under
        # config["configurable"] from _parent_config()) into State as a JSON
        # string (the state contract keeps every value msgpack-safe) so the
        # no-arg CallGitLabApiNode can read it via state.get("gitlab_config") -
        # and seed the validated caller contract stashed on the context bridge
        # as `gitlab_request`.
        configurable = self.config.get("configurable") or {}
        gitlab = configurable.get("gitlab") or {}
        extra: dict[str, Any] = {}
        if gitlab:
            extra["gitlab_config"] = to_json(gitlab)
        gitlab_request = get_caller_gitlab_request()
        if gitlab_request:
            extra["gitlab_request"] = gitlab_request
        return extra

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "project_id": state.get("project_id", ""),
            "resource_id": state.get("resource_id", ""),
            "resource_ref": state.get("resource_ref", ""),
            "resource_title": state.get("resource_title", ""),
            "confirmation": state.get("confirmation", ""),
            "gitlab_payload": state.get("gitlab_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
