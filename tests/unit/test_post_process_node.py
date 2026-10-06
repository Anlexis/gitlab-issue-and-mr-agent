# CMN-C2-239 - Unit tests: PostProcessNode (outer backbone, domain output gate).
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline); this backbone formatter declares ANONYMOUS -> the state builder
# sets caller_trust_level = TrustLevel.ANONYMOUS.value. The domain output gate
# is the MODULE-LEVEL _security_gate_output() helper (the framework gate
# methods are @final and the SDK auto-wraps _extra_ hooks), so the helper is
# also unit-tested directly as a plain function - including its recursive walk
# of nested containers.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials

from src.nodes.post_process_node import (
    ERROR_REASONS,
    _EVIDENCE_STATE_FIELDS,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    PostProcessNode,
    _security_gate_output,
)
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "resource_id": "4321",
        "resource_ref": "gitlab://example-group/webapp/issues/4321",
        "resource_title": "login fails on safari",
        "intent": "create_issue",
        "confirmation": "Created GitLab issue 'login fails on safari' - ref=gitlab://example-group/webapp/issues/4321 - id=4321",
        "gitlab_payload": to_json({"title": "login fails on safari"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["resource_id"] == "4321"
        assert out["resource_ref"] == "gitlab://example-group/webapp/issues/4321"
        assert out["intent"] == "create_issue"
        assert out["confirmation"].startswith("Created GitLab issue")

    def test_raw_request_payload_is_never_surfaced(self):
        # Output containment: the assembled request dict stays internal - the
        # caller sees scalar evidence fields + the confirmation only.
        result = self.node(_state())
        assert "gitlab_payload" not in result["formatted_output"]

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            resource_id="",
            resource_ref="",
            error_log=["CallGitLabApiNode: GitLab API error 403: forbidden"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "GitLab API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_resource_evidence(self):
        """Full node path: a SUCCESS output missing resource_id/resource_ref is blocked."""
        result = self.node(_state(resource_id="", resource_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])

    def test_audit_emits_outcome_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.post_process_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - outcome signals only.
        assert payloads["post_process_complete"] == {"intent": "create_issue", "has_resource_id": True}


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_resource_evidence(self):
        violations = _security_gate_output(
            {"resource_id": "4321", "resource_ref": "gitlab://example-group/webapp/issues/4321", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_resource_evidence(self):
        violations = _security_gate_output(
            {"resource_id": "", "resource_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "resource_id/resource_ref" in violations[0]

    def test_blocks_bearer_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"resource_id": "4321", "note": bearer_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_blocks_gitlab_pat_shaped_value(self):
        pat_like = "glpat-" + "a" * 20
        violations = _security_gate_output(
            {"resource_id": "4321", "note": pat_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"resource_id": "", "resource_ref": ""}, is_success=False)
        assert violations == []

    def test_nested_credential_is_caught(self):
        # The gate walks NESTED structures: a credential riding one level down
        # inside a surfaced container must still be blocked.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"resource_id": "4321", "details": {"args": {"text": bearer_like}}},
            is_success=True,
        )
        assert any("details" in v for v in violations)

    def test_nested_credential_in_a_list_is_caught(self):
        pat_like = "glpat-" + "a" * 20
        violations = _security_gate_output(
            {"resource_id": "4321", "notes": [{"body": pat_like}]},
            is_success=True,
        )
        assert any("notes" in v for v in violations)

    def test_top_level_control_still_fires(self):
        # The control that proves the recursive verifier itself works: the
        # same credential at the TOP level is also caught (a nested-only
        # failure would mean the probe, not the gate, was broken).
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"resource_id": "4321", "note": bearer_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_clean_nested_structure_passes(self):
        violations = _security_gate_output(
            {"resource_id": "4321", "details": {"labels": ["bug", "ops"], "iid": 4321}},
            is_success=True,
        )
        assert violations == []


class TestGateIsNeverNarrowerThanThePlatformScan:
    """The domain gate must catch everything detect_credentials() catches.

    A shape the framework knows and this gate misses is a BYPASS, not a lighter
    second line of defence: the value passes here, the @final
    FunctionNode._security_gate_output raises on the same delta a moment later,
    and BaseNode.__call__ discards the node's whole dict - the evidence
    clearing in _contained_error_delta() with it. The state then keeps the
    refused answer and nothing was contained.

    Samples are assembled at runtime so no credential-shaped literal is
    committed.
    """

    # One sample per pattern the platform detector ships.
    PLATFORM_SAMPLES = {
        "stripe_key": "sk_" + "live_" + "a" * 16,
        "openai_key": "sk-" + "a" * 24,
        "jwt": "eyJ" + "abcdefghij.k",
        "aws_key": "AKIA" + "A1B2C3D4E5F6G7H8",
        "bearer_token": "Bearer " + "a" * 24,
        # Split so no credential-shaped substring is committed on one line
        # (scripts/check_credentials.py scans the source, not the runtime value).
        "conn_string": "".join(("postgres", "ql://", "user", ":", "pw", "@db.internal/records")),
    }

    @pytest.mark.parametrize("sample", PLATFORM_SAMPLES.values(), ids=list(PLATFORM_SAMPLES))
    def test_the_platform_detector_really_knows_this_sample(self, sample):
        # Verify the verifier: a "parity" list of strings the framework does
        # not itself detect would make every assertion below vacuous.
        assert detect_credentials(sample)

    @pytest.mark.parametrize("sample", PLATFORM_SAMPLES.values(), ids=list(PLATFORM_SAMPLES))
    def test_the_domain_gate_blocks_it_too(self, sample):
        violations = _security_gate_output({"resource_id": "4321", "note": sample}, is_success=True)
        assert any("note" in v for v in violations)

    @pytest.mark.parametrize("sample", PLATFORM_SAMPLES.values(), ids=list(PLATFORM_SAMPLES))
    def test_the_error_envelope_never_repeats_it(self, sample):
        # The envelope is a closed set: an error_log line carrying the sample
        # is not redacted into the envelope - it is not projected at all.
        result = PostProcessNode().execute(
            _state(status=AgentStatus.ERROR.value, error_log=[f"SomeNode: upstream rejected {sample}"])
        )
        assert result["formatted_output"] == {"status": AgentStatus.ERROR.value, "reason": _REASON_WORKFLOW_FAILED}
        assert sample not in json.dumps(result, default=str)

    def test_a_shape_only_the_local_patterns_know_is_still_blocked(self):
        # The union runs both ways. Delegating to the platform detector INSTEAD
        # of the local patterns would silently narrow this gate: a GitLab PAT is
        # the credential this agent's domain makes likely and the platform
        # detector does not recognise it.
        pat_like = "glpat-" + "a" * 20
        assert not detect_credentials(pat_like)
        violations = _security_gate_output({"resource_id": "4321", "note": pat_like}, is_success=True)
        assert any("note" in v for v in violations)

    def test_ordinary_gitlab_content_still_passes(self):
        # The widening must not start refusing the product: real references,
        # branch names and issue prose carry none of these shapes.
        violations = _security_gate_output(
            {
                "resource_id": "4321",
                "resource_ref": "gitlab://example-group/webapp/merge_requests/87",
                "resource_title": "Bump redis client to 5.0.1",
                "confirmation": "Opened merge request 'Bump redis client to 5.0.1' - id=87",
            },
            is_success=True,
        )
        assert violations == []


class TestClearedSetInventory:
    """Every field the graph node merges into outer state is accounted for.

    The cleared set is a hand-maintained tuple, so a field added to
    GitLabWorkflowGraphNode.merge_output() later can silently miss it and ride
    out on a checkpoint or a downstream reader. This pins the correspondence
    instead of the tuple: each merged key must be either cleared on a refusal,
    a control channel, or named inert here on purpose. A new key is a test
    failure until somebody decides which it is.
    """

    # Channels that carry the refusal itself, not the refused product.
    CONTROL_KEYS = {"status", "error_log"}
    # Content-free by construction, so they may survive a refusal:
    #   intent          - one of three fixed verbs classified from the request
    #   project_id      - the caller's own project reference, echoed back
    #   redaction_flags - JSON list of pattern CATEGORY names, never values
    INERT_KEYS = {"intent", "project_id", "redaction_flags"}

    def _merged_keys(self):
        from src.graph.graph import GitLabWorkflowGraphNode

        return set(GitLabWorkflowGraphNode().merge_output({}, {}))

    def test_every_merged_key_is_cleared_control_or_declared_inert(self):
        cleared = set(_EVIDENCE_STATE_FIELDS) | {"result"}
        unaccounted = self._merged_keys() - cleared - self.CONTROL_KEYS - self.INERT_KEYS
        assert unaccounted == set(), (
            "merge_output() writes caller-facing field(s) the error path neither "
            f"clears nor declares inert: {sorted(unaccounted)}"
        )

    def test_the_cleared_set_only_names_fields_that_exist(self):
        # The other direction: a cleared field that nothing merges is dead
        # weight, and hides the fact that the real one was never covered.
        assert (set(_EVIDENCE_STATE_FIELDS) | {"result"}) <= self._merged_keys()

    def test_the_declared_inert_keys_really_are_merged(self):
        # Verify the verifier: an INERT_KEYS entry that no longer appears in
        # merge_output() would silently absorb a future field of the same name.
        assert self.INERT_KEYS <= self._merged_keys()


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. The token is
    assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


# Fragments of the sentinel that must survive nowhere in a returned mapping.
_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")

# Every value the ERROR envelope may carry: the error status and the reason codes.
_DECLARED_ENVELOPE_VALUES = ERROR_REASONS | {AgentStatus.ERROR.value}


def _leaves(value):
    """Every key and scalar inside `value`, rendered as text, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
    else:
        yield str(value)


# Every non-success path this node has. `via` says how the path is reached:
# node(state) runs the framework pipeline; execute() is used for an errored
# state because BaseNode.__call__ short-circuits on it (and the backbone routes
# an error straight to finalize), so that branch is only reachable from inside.
_ERROR_PATHS = [
    pytest.param(
        {"status": AgentStatus.ERROR.value, "resource_id": "", "resource_ref": ""},
        "execute",
        id="inner-workflow-error",
    ),
    pytest.param(
        {"status": AgentStatus.ERROR.value, "result": {"resource_id": "4321"}},
        "execute",
        id="inner-workflow-error-with-answer-in-result",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "error_log": [_sentinel(), "CallGitLabApiNode: rejected glpat-" + "a" * 20],
        },
        "execute",
        id="inner-workflow-error-with-credential-in-error-log",
    ),
    pytest.param({"resource_id": "", "resource_ref": ""}, "call", id="gate-missing-resource-evidence"),
    pytest.param({"confirmation": "rotate glpat-" + "a" * 20}, "call", id="gate-credential-shaped-value"),
]


class TestErrorEnvelopeIsClosedSet:
    """Whatever the non-success path, the caller-visible envelope is made of
    this module's own constants - never of node-authored text.

    error_log is seeded with a recognisable sentinel on every path: a name and
    a credential-shaped token inside an echoed upstream body, which is exactly
    what an API-call failure can put there. Truncating or redacting such a line
    is not a closed set, so it must appear nowhere in what the node returns.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _drive(self, overrides: dict, via: str) -> dict:
        state = _state(**{"error_log": [_sentinel()], **overrides})
        return self.node.execute(state) if via == "execute" else self.node(state)

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_values_are_declared_constants(self, overrides, via):
        result = self._drive(overrides, via)

        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == {"status", "reason"}, envelope
        assert set(envelope.values()) <= _DECLARED_ENVELOPE_VALUES, envelope

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_stays_truthy(self, overrides, via):
        """`formatted_output or result`: a falsy envelope re-opens the fallback."""
        result = self._drive(overrides, via)

        assert result["formatted_output"], "a falsy formatted_output re-opens the `result` fallback"
        assert result["result"] is None

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_seeded_error_text_appears_nowhere_in_the_returned_mapping(self, overrides, via):
        result = self._drive(overrides, via)

        leaves = list(_leaves(result))
        for fragment in _SENTINEL_FRAGMENTS:
            assert not any(fragment in leaf for leaf in leaves), (fragment, result)
        rendered = json.dumps(result, default=str)
        assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered

    def test_the_reason_names_the_path_taken(self):
        errored = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))
        withheld = self.node(_state(resource_id="", resource_ref="", error_log=[_sentinel()]))

        assert errored["formatted_output"]["reason"] == _REASON_WORKFLOW_FAILED
        assert withheld["formatted_output"]["reason"] == _REASON_OUTPUT_WITHHELD

    def test_inner_error_entries_are_not_re_emitted(self):
        """The state reducer appends error_log; re-emitting the inner entries
        would duplicate every line, and the caller never sees them anyway."""
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))

        assert "error_log" not in result

    def test_gate_violations_travel_in_error_log_only(self):
        result = self.node(_state(resource_id="", resource_ref="", error_log=[_sentinel()]))

        assert any("output gate" in entry for entry in result["error_log"])
        assert "output gate" not in json.dumps(result["formatted_output"])

    def test_the_probe_finds_the_sentinel_where_it_lives(self):
        # Verify the verifier: the same walk DOES find the sentinel in a
        # mapping that carries it, so the "nowhere" assertions are not vacuous.
        assert any("A. Tanaka" in leaf for leaf in _leaves({"error_log": [_sentinel()]}))
