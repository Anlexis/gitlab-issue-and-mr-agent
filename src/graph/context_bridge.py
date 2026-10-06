"""AgentCore Platform v1.0 - caller-context bridge across the graph boundary."""

# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` and does NOT forward
# the outer state's input_context, so an inner-node read of
# state["input_context"] would always see {} through the nested graph. The
# sanctioned subclass hooks bridge it:
#
#   GitLabWorkflowGraphNode.extract_input(state)   [runs BEFORE subgraph.invoke]
#       -> set_caller_gitlab_request(state["gitlab_request"])
#   GitLabWorkflowGraph._extra_initial_state()     [runs INSIDE subgraph.invoke]
#       -> returns {"gitlab_request": <the stashed JSON>}
#
# What crosses the bridge is the VALIDATED caller contract produced by
# PreProcessNode - never the raw request body - so the inner graph is only
# ever handed fields that already passed their type, shape and length bounds.
#
# The alternative, smuggling caller data inside the validated_input JSON, is
# not usable here: the framework masks that field at every node boundary, so
# caller text can be rewritten between hops (a Title-Case issue title inside
# validated_input arrives at the API call as "[MASKED]"). This channel is not
# masked, which is also why PreProcessNode screens and bounds every field
# itself before anything enters it.
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's context.

from contextvars import ContextVar
from typing import Optional

_CALLER_GITLAB_REQUEST: ContextVar[Optional[str]] = ContextVar("cmn_c2_239_caller_gitlab_request", default=None)


def set_caller_gitlab_request(gitlab_request: Optional[str]) -> None:
    """Stash the validated caller contract (JSON) for the imminent inner-graph invoke."""
    _CALLER_GITLAB_REQUEST.set(gitlab_request or None)


def get_caller_gitlab_request() -> Optional[str]:
    """Read (without consuming) the stashed contract JSON; None when unset."""
    return _CALLER_GITLAB_REQUEST.get()
