"""AgentCore Platform v1.0 - CMN-C2-239 GitLab Issue & MR Agent state."""

# State contract: State must be a flat TypedDict - never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects (and
# nested dict/list containers) are not msgpack-safe. Extend AgentState with
# agent-specific fields only, and declare every domain field NotRequired[...]
# (fields are absent until their producer node writes them). gitlab_payload /
# gitlab_config / gitlab_request / redaction_flags are dicts/lists at the
# point of use but are stored in State as JSON strings via to_json/from_json
# below. Do NOT add credentials, secrets, or Pydantic models. The GitLab
# integration token is NEVER stored here - it is read via ctx.secrets in
# CallGitLabApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """GitLab Issue & MR agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only GitLab-workflow fields are added below, all NotRequired (state
    contract - fields are absent until their producer writes them). All
    values are JSON/msgpack-serializable primitives - the GitLab integration
    token is NEVER stored here (accessed via ctx.secrets).
    """

    # Caller-supplied target project (validated by pre_process from
    # input_context). Never inferred; resolution to a GitLab project is
    # explicit-only (text mention > this field > 'Project:' request line).
    project_hint: NotRequired[str]
    project_id: NotRequired[str]  # resolved GitLab project path or numeric id

    # Validated structured caller contract (pre_process): project / intent /
    # title / description / comment / labels / source_branch / target_branch /
    # target_kind / target_iid, every field type-, shape- and bound-checked
    # before it is stored. JSON string; crosses the graph boundary via the
    # context bridge (src/graph/context_bridge.py).
    gitlab_request: NotRequired[Optional[str]]

    # ValidateInput (deterministic input scan)
    # JSON list[str] of pattern categories redacted from the text before
    # logging (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferGitLabFields
    resource_title: NotRequired[str]  # issue / merge-request title (or comment target label)
    # JSON - assembled GitLab REST API v4 request body (stored as a JSON
    # string, not a native dict; (de)serialize via to_json/from_json).
    gitlab_payload: NotRequired[Optional[str]]

    # Runtime `gitlab:` section (config/config.yaml) forwarded by
    # _parent_config() and injected by the inner graph's
    # _extra_initial_state() (JSON string).
    gitlab_config: NotRequired[Optional[str]]

    # CallGitLabApi
    resource_id: NotRequired[str]  # iid / note id returned by GitLab
    resource_ref: NotRequired[str]  # human-readable reference (gitlab://<project>/issues/<iid>)

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
