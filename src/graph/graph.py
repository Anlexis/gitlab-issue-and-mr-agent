"""AgentCore Platform v1.0 - CMN-C2-239 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in GitLabWorkflowGraphNode (`main`
slot), which wraps the inner GitLabWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

from pathlib import Path
from typing import Any, ClassVar

import yaml

from framework.errors import SubgraphError
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_gitlab_request
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import _REASON_OUTPUT_WITHHELD, PostProcessNode, _security_gate_output
from src.schemas.state import State

# Repo-root runtime config: src/graph/graph.py -> parents[2] = repo root.
# config/agent.yaml is the static registry manifest (flat, identity keys only);
# every runtime value lives in config/config.yaml.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def load_runtime_config() -> "dict[str, Any]":
    """Load config/config.yaml (max_retry, timeout_s, gitlab integration block).

    Shared by the standalone entry point (which passes it to the agent
    constructor so the backbone's retry settings actually apply) and by
    GitLabWorkflowGraphNode._parent_config() (which forwards the `gitlab:`
    block to the inner graph). A missing/unreadable file degrades to {} - the
    inner nodes then fall back to the documented defaults (default base_url +
    the network-free stub transport).
    """
    try:
        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


class GitLabWorkflowGraphNode(GraphNode):
    """Wraps the inner GitLab workflow graph; assigned to the `main` slot.

    No constructor arguments (SDK v1 nodes are no-arg) - configuration reaches
    the subgraph via _parent_config(), which loads the runtime config file.
    """

    # An inner-graph rejection is a NORMAL domain outcome here (unsupported
    # operation, unresolved project, missing title - caller mistakes, not
    # bugs), so it is returned as a structured error envelope rather than
    # re-raised: the default "propagate" strategy would turn every caller typo
    # into an exception whose handler records the full Python traceback -
    # runtime file paths included - in error_log, and would drop the domain
    # message the inner node wrote.
    error_strategy: ClassVar[str] = "handle"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> Any:
        from src.graph.domain_workflow_graph import GitLabWorkflowGraph

        return GitLabWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the request into validated_input (JSON string).
        # The validated structured caller fields cross into the inner graph via
        # the context bridge (src/graph/context_bridge.py): this hook runs
        # immediately before subgraph.invoke(), which does not forward them.
        gitlab_request = state.get("gitlab_request")
        set_caller_gitlab_request(gitlab_request if isinstance(gitlab_request, str) else None)
        return str(state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "project_id": sub_result.get("project_id", ""),
            "resource_id": sub_result.get("resource_id", ""),
            "resource_ref": sub_result.get("resource_ref", ""),
            "resource_title": sub_result.get("resource_title", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "gitlab_payload": sub_result.get("gitlab_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def on_subgraph_error(self, state: AgentState, error: Exception) -> "dict[str, Any]":
        """Return the inner graph's own domain messages; never a traceback.

        A SubgraphError wraps the inner error_log (already field-naming,
        value-free messages written by the inner nodes) - surface those. Any
        other exception type is unexpected, so the caller gets a generic
        message: exception text can carry runtime paths and internals that
        must not reach an external caller.
        """
        if isinstance(error, SubgraphError) and error.error_log:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": list(error.error_log),
            }
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": ["GitLabWorkflowGraphNode: inner workflow failed"],
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the runtime config to the inner graph under config["configurable"].

        Loads config/config.yaml and forwards the `gitlab:` integration
        section - never an empty {} while the file declares one (an empty
        _parent_config() would silently disconnect every declared setting).
        The backbone-level keys (max_retry, timeout_s) are consumed by the
        OUTER graph via its constructor config (see load_runtime_config) and
        are deliberately not forwarded: no inner node reads them.
        """
        runtime = load_runtime_config()
        configurable: dict[str, Any] = {}
        if isinstance(runtime.get("gitlab"), dict):
            configurable["gitlab"] = runtime["gitlab"]
        return {"configurable": configurable}


class GitLabIssueMRAgent(AgentBaseGraph):
    """CMN-C2-239 outer graph - GitLab Issue & MR Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in GitLabWorkflowGraphNode (`main` slot); GitLab
    settings flow from config/config.yaml via _parent_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_239"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = GitLabWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        """Structured product (docs/02 "Structured Output"): EXTEND the base
        envelope with the structured action keys - never replace it.

        Structured keys are surfaced ONLY on SUCCESS; before surfacing they are
        re-scanned by the module-level output gate and any violation fails
        closed (structured keys suppressed, status=error).

        On a non-SUCCESS status the base envelope's `output` slot is emptied.
        AgentBaseGraph.get_output() fills that slot from `formatted_output or
        result` with NO status check, and on an error path the surviving state
        can still hold the merged inner output (resource id / ref /
        confirmation) written before the failure - so the caller being told the
        GitLab action FAILED would receive the evidence that it happened.
        Suppressing the structured keys alone never closed that; emptying the
        slot does, and matches the fail-closed shape used below.

        The caller-visible error is a closed set. When the re-scan refuses the
        response the envelope carries a constant `reason` code and nothing
        else: neither `error_log` nor the gate's violation entries are
        projected. Both are node-authored text (an API-call failure puts an
        upstream response body in the log), so they stay internal.
        """
        output: dict[str, Any] = super().get_output(state)
        if state.get("status") != AgentStatus.SUCCESS.value:
            # Error/other paths: base envelope only, no structured keys - and
            # no `formatted_output or result` passthrough of action evidence.
            output["output"] = None
            return output

        structured = {
            "intent": state.get("intent", ""),
            "project_id": state.get("project_id", ""),
            "resource_id": state.get("resource_id", ""),
            "resource_ref": state.get("resource_ref", ""),
            "resource_title": state.get("resource_title", ""),
            "confirmation": state.get("confirmation", ""),
        }

        # Fail-closed re-scan of the structured surface (same module-level
        # domain gate as PostProcessNode - see src/nodes/post_process_node.py).
        violations = _security_gate_output(structured, is_success=True)
        if violations:
            output["status"] = AgentStatus.ERROR.value
            output["output"] = None
            # Closed-set label only - never error_log or the violation text.
            output["reason"] = _REASON_OUTPUT_WITHHELD
            return output

        output.update(structured)
        return output
