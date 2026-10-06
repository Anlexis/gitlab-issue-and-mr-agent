# CMN-C2-239 - PB: error-envelope containment on the two PostProcessNode error paths.
#
# A code review of this node reported that both the existing-ERROR path and the
# output-gate-violation path left action evidence / merged output reachable by
# the caller.
#
# What the two paths did:
#
#   Path A - incoming-ERROR branch. formatted_output was rebuilt by reading
#     resource_id / resource_ref straight back out of the SAME state it was
#     meant to contain. Those two fields ARE the GitLab write evidence - the
#     node's own output gate REFUSES a SUCCESS that lacks them - so an error
#     envelope told a caller being informed of failure that a write had
#     occurred and exactly which record it touched.
#
#   Path B - output-gate-violation branch. The delta carried NO
#     formatted_output at all. AgentBaseGraph.get_output() projects
#     `formatted_output or result` with NO status check, so an absent/falsy
#     formatted_output re-opens the fallback onto `result` - the merged inner
#     output (resource id + ref + confirmation) - which is then shipped under
#     an ERROR envelope. The gate refuses to publish the mapping and the
#     framework publishes the same content from the field next door.
#
# Both are containment in form only. The fix has two layers and both are
# asserted here:
#
#   1. PostProcessNode ships a PRESENT AND TRUTHY formatted_output on both
#      error paths (a falsy one is what re-activates the fallback) carrying
#      the error status + a constant reason code only - a closed set, never
#      error_log or the gate's violation entries - and BLANKS the evidence
#      fields in state so a checkpoint or downstream reader cannot pick them
#      up either.
#   2. GitLabIssueMRAgent.get_output() empties the envelope's `output` slot on
#      any non-SUCCESS status instead of passing `formatted_output or result`
#      through - which also covers the error paths where post_process never
#      runs at all (an errored `main` routes straight to finalize).
#
# Every containment assertion is paired with a clean-path control
# (TestCleanPathControl) so it cannot pass vacuously: the same probe MUST still
# find the evidence on the success path.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import GitLabIssueMRAgent
from src.nodes.post_process_node import ERROR_REASONS, _REASON_OUTPUT_WITHHELD, PostProcessNode
from src.schemas.state import to_json

# ── the evidence a caller must never receive under an ERROR envelope ──────────
RESOURCE_ID = "4321"
RESOURCE_REF = "gitlab://example-group/webapp/issues/4321"
RESOURCE_TITLE = "login fails on safari"
CONFIRMATION = f"Created GitLab issue '{RESOURCE_TITLE}' - ref={RESOURCE_REF} - id={RESOURCE_ID}"
# The merged inner output written by ConfirmNode and mapped into the outer
# state by GitLabWorkflowGraphNode.merge_output().
MERGED_RESULT = {
    "resource_id": RESOURCE_ID,
    "resource_ref": RESOURCE_REF,
    "confirmation": CONFIRMATION,
}
EVIDENCE_TOKENS = (RESOURCE_ID, RESOURCE_REF, RESOURCE_TITLE, CONFIRMATION)


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _walk_strings(value):
    """Yield every string reachable inside value (dict/list/tuple recursion)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_strings(child)


def _leaked(blob):
    """Evidence tokens reachable anywhere inside blob (the probe)."""
    texts = list(_walk_strings(blob))
    return [token for token in EVIDENCE_TOKENS if any(token in text for text in texts)]


def _state(**overrides) -> dict:
    """Outer-backbone state as it stands when post_process runs: the GitLab
    write has happened, so every evidence field is populated."""
    state = {
        "user_input": f"Create an issue titled '{RESOURCE_TITLE}' in project example-group/webapp.",
        "status": AgentStatus.SUCCESS.value,
        "intent": "create_issue",
        "project_id": "example-group/webapp",
        "resource_id": RESOURCE_ID,
        "resource_ref": RESOURCE_REF,
        "resource_title": RESOURCE_TITLE,
        "confirmation": CONFIRMATION,
        "result": dict(MERGED_RESULT),
        "gitlab_payload": to_json({"title": RESOURCE_TITLE}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "pb-containment",
        "trace_id": "pb-containment-trace",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _apply(state: dict, delta: dict) -> dict:
    """Merge a node delta into state the way the graph runtime does
    (error_log / node_history use operator.add reducers; the rest overwrite)."""
    merged = dict(state)
    for key, value in delta.items():
        if key in ("error_log", "node_history"):
            merged[key] = list(merged.get(key) or []) + list(value or [])
        else:
            merged[key] = value
    return merged


def _shipped(state: dict):
    """What the caller actually receives: the agent's own response envelope."""
    return GitLabIssueMRAgent().get_output(state)


# ─────────────────────────────────────────────────────────────────────────────
# Path A - incoming-ERROR branch
#
# Reached through PostProcessNode.execute(). Note the backbone normally routes
# an errored `main` straight to finalize and BaseNode.__call__ short-circuits
# on an already-errored state, so this branch is the node's defensive contract
# rather than the hot path - but it is exported, exercised here, and it is the
# source the review named.
# ─────────────────────────────────────────────────────────────────────────────
class TestIncomingErrorPath:
    def setup_method(self):
        self.node = PostProcessNode()
        self.state = _state(
            status=AgentStatus.ERROR.value,
            error_log=["ConfirmNode: post-write verification failed"],
        )
        self.delta = self.node.execute(self.state)

    def test_envelope_is_present_and_truthy(self):
        # A falsy formatted_output is what re-activates the framework's
        # `formatted_output or result` fallback - containment REQUIRES a
        # non-empty mapping here, not an absent or empty one.
        assert "formatted_output" in self.delta
        assert self.delta["formatted_output"]

    def test_envelope_carries_no_action_evidence(self):
        assert _leaked(self.delta["formatted_output"]) == []

    def test_evidence_is_cleared_from_state_not_merely_omitted(self):
        # Omitting the identifiers from one envelope is not containment: a
        # checkpoint or downstream reader picks them straight back out of state.
        after = _apply(self.state, self.delta)
        for field in ("resource_id", "resource_ref", "resource_title", "confirmation"):
            assert not after.get(field), field
        assert not after.get("result")

    def test_shipped_envelope_carries_no_action_evidence(self):
        assert _leaked(_shipped(_apply(self.state, self.delta))) == []

    def test_shipped_output_slot_is_empty(self):
        # Layer 2: get_output() must not pass `formatted_output or result`
        # through on a non-SUCCESS status.
        assert not _shipped(_apply(self.state, self.delta))["output"]

    def test_error_status_is_still_reported(self):
        after = _apply(self.state, self.delta)
        assert self.delta["status"] == AgentStatus.ERROR.value
        assert _shipped(after)["status"] == AgentStatus.ERROR.value
        assert any("verification failed" in entry for entry in after["error_log"])


# ─────────────────────────────────────────────────────────────────────────────
# Path B1 - output-gate violation: SUCCESS with no resource evidence
#
# The gate's own trigger condition. The scalar evidence fields failed to map
# but the merged inner output survived in `result`, so the framework projection
# shipped it under the ERROR envelope.
# ─────────────────────────────────────────────────────────────────────────────
class TestGateViolationMissingEvidence:
    def setup_method(self):
        self.node = PostProcessNode()
        self.state = _state(resource_id="", resource_ref="")
        self.delta = self.node.execute(self.state)

    def test_gate_actually_fired(self):
        assert self.delta["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in self.delta["error_log"])

    def test_envelope_is_present_and_truthy(self):
        assert "formatted_output" in self.delta
        assert self.delta["formatted_output"]

    def test_envelope_carries_no_record_content(self):
        assert _leaked(self.delta["formatted_output"]) == []

    def test_merged_output_is_cleared_from_state(self):
        after = _apply(self.state, self.delta)
        assert not after.get("result")
        assert not after.get("confirmation")
        assert not after.get("resource_title")

    def test_shipped_envelope_carries_no_record_content(self):
        assert _leaked(_shipped(_apply(self.state, self.delta))) == []

    def test_shipped_output_slot_is_empty(self):
        assert not _shipped(_apply(self.state, self.delta))["output"]

    def test_violation_messages_name_fields_not_values(self):
        joined = "\n".join(self.delta["error_log"])
        assert _leaked(joined) == []


# ─────────────────────────────────────────────────────────────────────────────
# Path B2 - output-gate violation: credential-shaped value in the output
#
# The second gate arm. A caller can put a token-shaped string in an issue
# title ("rotate glpat-..."), so it reaches resource_title -> confirmation.
# The gate refuses to publish formatted_output; the framework then published
# the SAME string out of `result`, which is the gate bypassed by fallback.
# ─────────────────────────────────────────────────────────────────────────────
class TestGateViolationCredentialShaped:
    def setup_method(self):
        # Built at runtime so no credential-shaped literal is committed.
        self.token_like = "glpat-" + "a" * 20
        tainted = f"Created GitLab issue 'rotate {self.token_like}' - id={RESOURCE_ID}"
        self.node = PostProcessNode()
        self.state = _state(
            resource_title=f"rotate {self.token_like}",
            confirmation=tainted,
            result={
                "resource_id": RESOURCE_ID,
                "resource_ref": RESOURCE_REF,
                "confirmation": tainted,
            },
        )
        self.delta = self.node.execute(self.state)

    def test_gate_actually_fired(self):
        assert self.delta["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in self.delta["error_log"])

    def test_credential_never_reaches_the_shipped_envelope(self):
        shipped = _shipped(_apply(self.state, self.delta))
        assert not any(self.token_like in text for text in _walk_strings(shipped))

    def test_credential_bearing_fields_are_cleared_from_state(self):
        after = _apply(self.state, self.delta)
        assert not any(self.token_like in text for text in _walk_strings(after.get("result")))
        assert not any(self.token_like in text for text in _walk_strings(after.get("confirmation")))
        assert not any(self.token_like in text for text in _walk_strings(after.get("resource_title")))

    def test_envelope_is_present_and_truthy(self):
        assert self.delta["formatted_output"]

    def test_violation_message_names_the_field_not_the_matched_value(self):
        joined = "\n".join(self.delta["error_log"])
        assert self.token_like not in joined
        assert "formatted_output[" in joined


# ─────────────────────────────────────────────────────────────────────────────
# Clean-path controls - without these the containment assertions above could
# pass vacuously (a probe that finds nothing anywhere proves nothing).
# ─────────────────────────────────────────────────────────────────────────────
class TestCleanPathControl:
    def setup_method(self):
        self.node = PostProcessNode()
        self.state = _state()
        self.delta = self.node.execute(self.state)

    def test_success_path_still_returns_the_answer(self):
        assert self.delta["status"] == AgentStatus.SUCCESS.value
        out = self.delta["formatted_output"]
        assert out["resource_id"] == RESOURCE_ID
        assert out["resource_ref"] == RESOURCE_REF
        assert out["confirmation"] == CONFIRMATION

    def test_success_path_keeps_the_evidence_in_state(self):
        after = _apply(self.state, self.delta)
        assert after["resource_id"] == RESOURCE_ID
        assert after["resource_ref"] == RESOURCE_REF

    def test_probe_finds_the_evidence_on_the_success_envelope(self):
        # The verifier verified: the same _leaked() probe used by every
        # containment assertion DOES fire on the success envelope.
        shipped = _shipped(_apply(self.state, self.delta))
        assert set(_leaked(shipped)) >= {RESOURCE_ID, RESOURCE_REF, CONFIRMATION}


# ─────────────────────────────────────────────────────────────────────────────
# The envelope is a closed set - on every non-success path, at the boundary the
# caller actually sees.
#
# error_log is seeded with a recognisable sentinel: a name and a credential-
# shaped token inside an echoed upstream body, which is what an API-call
# failure can put there. Truncating or redacting such a line is not a closed
# set; it must reach neither the node's delta nor the shipped envelope - on the
# incoming-ERROR path, on both gate arms, and on the get_output() re-scan
# refusal (a credential-shaped project_id is the one surface only that layer
# sees, and that refusal used to copy error_log into the envelope).
# ─────────────────────────────────────────────────────────────────────────────
def _sentinel() -> str:
    # Assembled at runtime so no credential-shaped literal is committed.
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")
_DECLARED_ENVELOPE_VALUES = ERROR_REASONS | {AgentStatus.ERROR.value}


def _seeded(blob) -> "list[str]":
    """Sentinel fragments reachable anywhere inside blob."""
    texts = list(_walk_strings(blob))
    return [fragment for fragment in _SENTINEL_FRAGMENTS if any(fragment in text for text in texts)]


def _error_states():
    token_like = "glpat-" + "a" * 20
    return [
        pytest.param(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]), id="incoming-error"),
        pytest.param(_state(resource_id="", resource_ref="", error_log=[_sentinel()]), id="gate-missing-evidence"),
        pytest.param(_state(confirmation=f"rotate {token_like}", error_log=[_sentinel()]), id="gate-credential-shaped"),
    ]


class TestErrorEnvelopeIsClosedSet:
    @pytest.mark.parametrize("state", _error_states())
    def test_node_delta_carries_the_declared_constants_only(self, state):
        delta = PostProcessNode().execute(state)
        envelope = delta["formatted_output"]
        assert set(envelope) == {"status", "reason"}, envelope
        assert set(envelope.values()) <= _DECLARED_ENVELOPE_VALUES, envelope
        assert _seeded(delta) == []

    @pytest.mark.parametrize("state", _error_states())
    def test_shipped_envelope_carries_no_seeded_text(self, state):
        delta = PostProcessNode().execute(state)
        shipped = _shipped(_apply(state, delta))
        assert shipped["status"] == AgentStatus.ERROR.value
        assert _seeded(shipped) == []

    def test_re_scan_refusal_ships_a_reason_code_and_no_error_log(self):
        state = _state(project_id="Bearer " + "a" * 24, error_log=[_sentinel()])
        delta = PostProcessNode().execute(state)
        assert delta["status"] == AgentStatus.SUCCESS.value  # post_process did not refuse; get_output() does
        shipped = _shipped(_apply(state, delta))
        assert shipped["status"] == AgentStatus.ERROR.value
        assert shipped["reason"] == _REASON_OUTPUT_WITHHELD
        assert "error_log" not in shipped
        assert _seeded(shipped) == []
        assert not any("output gate" in text for text in _walk_strings(shipped))

    def test_the_probe_finds_the_sentinel_where_it_lives(self):
        # Verify the verifier: the same walk DOES find the sentinel in the state
        # it was seeded into, so the "nowhere" assertions above are not vacuous.
        assert set(_seeded(_state(error_log=[_sentinel()]))) == set(_SENTINEL_FRAGMENTS)
