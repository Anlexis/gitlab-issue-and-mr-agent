# CMN-C2-239 - E2E: a refused output must not come back through the framework's
# `formatted_output or result` fallback.
#
# AgentBaseGraph.get_output() projects `state["formatted_output"] or
# state["result"]` with NO status check. So an output gate that returns
# status=ERROR without writing a truthy formatted_output does not withhold the
# answer - it hands the caller the pre-gate value sitting in `result` under an
# error envelope.
#
# WHY THESE TESTS DRIVE THE WHOLE GRAPH
#
# The containment assertions this repo already ships
# (tests/proof_of_boundary/test_pb_error_envelope_containment.py) call
# PostProcessNode.execute() on hand-built state and merge the delta with a
# local helper. That cannot observe this defect: the fallback lives in
# get_output() over the state the RUNTIME assembled, and the reducer semantics
# (a key omitted from a delta keeps its previous value) are LangGraph's, not
# the helper's. Everything below therefore drives either the full outer graph
# via GitLabIssueMRAgent.invoke() or the real ASGI /invoke handler, and asserts
# on the complete response.
#
# Faults are injected on the DATA path only - caller `input_context`, which is
# where a real request's project reference comes from. No gate, node or graph
# module is patched; the pipeline, the gates and the envelope are shipped code
# throughout. The one monkeypatch is a read-only spy on get_output() that
# records the final outer state and calls the real implementation unchanged, so
# the tests can assert on state as well as on the envelope.
#
# Every containment assertion is paired with a clean-path control, so a
# refuse-everything regression cannot make this file pass.

import importlib
import warnings

import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import GitLabIssueMRAgent, load_runtime_config

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Warning)
        from starlette.testclient import TestClient

    _CLIENT_ERROR = None
except Exception as exc:  # pragma: no cover - stripped-down envs only
    TestClient = None
    _CLIENT_ERROR = exc


_HTTP_TOKEN = "containment-e2e-token"
_PROJECT = "example-group/webapp"
_TITLE = "Quarterly Rollout Plan"

# Built at runtime so no credential-shaped literal is committed. A GitLab PAT
# shape is the credential this agent's own domain makes likely, and it is a
# shape the PLATFORM detector does not know - so it survives every framework
# scan upstream and the template's own gate at post_process is the layer that
# has to catch it. That is what makes it the right probe for this defect.
TOKEN_LIKE = "glpat-" + "b" * 20

# Caller-facing evidence of the GitLab write. None of it may appear in the
# response once the gate has refused.
BLOCKED_MARKERS = (TOKEN_LIKE, f"gitlab://{TOKEN_LIKE}", "Created GitLab issue")

_OUTPUT_BEARING_STATE_FIELDS = (
    "result",
    "resource_id",
    "resource_ref",
    "resource_title",
    "confirmation",
    "gitlab_payload",
)
_STRUCTURED_ENVELOPE_KEYS = (
    "intent",
    "project_id",
    "resource_id",
    "resource_ref",
    "resource_title",
    "confirmation",
)


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


def _found(blob, *needles):
    """The needles reachable anywhere inside blob - the probe used everywhere below."""
    texts = list(_walk_strings(blob))
    return [n for n in needles if any(n in text for text in texts)]


def _request(project: str, title: str = _TITLE) -> dict:
    return {
        "input": "Please create an issue for the rollout",
        "session_id": "containment-e2e",
        "input_context": {"project": project, "intent": "create_issue", "title": title},
    }


@pytest.fixture()
def agent():
    graph = GitLabIssueMRAgent(config=load_runtime_config())
    graph.compile()
    return graph


@pytest.fixture()
def final_state(monkeypatch):
    """Records the outer state get_output() is called with; calls through unchanged."""
    captured: dict = {}
    original = GitLabIssueMRAgent.get_output

    def spy(self, state):
        captured["state"] = dict(state)
        return original(self, state)

    monkeypatch.setattr(GitLabIssueMRAgent, "get_output", spy)
    return captured


def _invoke(graph, project: str, title: str = _TITLE) -> dict:
    payload = _request(project, title)
    ctx = InvocationContext(
        session_id=payload["session_id"],
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )
    return graph.invoke(payload["input"], ctx=ctx, input_context=payload["input_context"])


@pytest.fixture()
def client(monkeypatch):
    if _CLIENT_ERROR is not None:  # pragma: no cover - stripped-down envs only
        pytest.skip(f"testclient unavailable: {_CLIENT_ERROR}")
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _HTTP_TOKEN)
    server = importlib.import_module("src.api.server")
    with TestClient(server.app) as tc:
        yield tc


def _post(client, project: str, title: str = _TITLE):
    return client.post(
        "/invoke",
        json=_request(project, title),
        headers={"Authorization": f"Bearer {_HTTP_TOKEN}"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# The blocked answer, through the full outer graph
#
# A project reference is a caller-supplied identifier that this template
# validates for SHAPE only - a PAT-shaped string is a shape-valid namespace
# path. It reaches the GitLab call, comes back inside resource_ref and the
# confirmation message, and the domain output gate refuses the response. What
# the caller receives next is the subject of these tests.
# ─────────────────────────────────────────────────────────────────────────────
class TestBlockedAnswerIsNotShippedByTheFallback:
    def test_status_is_error(self, agent):
        assert _invoke(agent, TOKEN_LIKE)["status"] == "error"

    def test_no_payload_is_shipped(self, agent):
        assert not _invoke(agent, TOKEN_LIKE).get("output")

    def test_nothing_of_the_refused_answer_reaches_the_caller(self, agent):
        assert _found(_invoke(agent, TOKEN_LIKE), *BLOCKED_MARKERS) == []

    def test_no_structured_action_keys_are_surfaced(self, agent):
        envelope = _invoke(agent, TOKEN_LIKE)
        assert [k for k in _STRUCTURED_ENVELOPE_KEYS if k in envelope] == []

    def test_the_block_happened_at_the_gate_not_upstream(self, agent):
        # Without this, a refusal anywhere earlier in the pipeline would satisfy
        # every assertion above while leaving the gate untested.
        assert "PostProcessNode" in _invoke(agent, TOKEN_LIKE)["node_history"]

    def test_the_persisted_state_is_cleared_not_merely_unpublished(self, agent, final_state):
        # LangGraph merges partial deltas: a key the node omits keeps its old
        # value, so a checkpoint or any later reader would still hold the
        # refused answer. Presence AND emptiness, not `not state.get(...)` on a
        # key that was never written.
        _invoke(agent, TOKEN_LIKE)
        state = final_state["state"]
        for field in _OUTPUT_BEARING_STATE_FIELDS:
            assert field in state, field
            assert not state[field], field
        assert _found({k: v for k, v in state.items() if k in _OUTPUT_BEARING_STATE_FIELDS}, *BLOCKED_MARKERS) == []

    def test_the_withheld_notice_is_truthy(self, agent, final_state):
        # "" and an absent key are both falsy and re-select `result` - the exact
        # fallback the gate exists to prevent.
        _invoke(agent, TOKEN_LIKE)
        assert final_state["state"]["formatted_output"]

    def test_the_refusal_reason_names_no_value(self, agent, final_state):
        _invoke(agent, TOKEN_LIKE)
        assert _found(final_state["state"]["formatted_output"], *BLOCKED_MARKERS) == []


class TestBlockedAnswerOverTheAsgiEntryPoint:
    def test_status_is_error(self, client):
        resp = _post(client, TOKEN_LIKE)
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"

    def test_no_payload_is_shipped(self, client):
        assert not _post(client, TOKEN_LIKE).json().get("output")

    def test_nothing_of_the_refused_answer_reaches_the_caller(self, client):
        assert _found(_post(client, TOKEN_LIKE).json(), *BLOCKED_MARKERS) == []

    def test_no_structured_action_keys_are_surfaced(self, client):
        body = _post(client, TOKEN_LIKE).json()
        assert [k for k in _STRUCTURED_ENVELOPE_KEYS if k in body] == []


# ─────────────────────────────────────────────────────────────────────────────
# An inner-workflow failure hands the caller closed-set labels, never text
#
# The request below names no comment target anywhere, so the workflow fails
# inside the inner graph. The failing node's error_log line names the node and
# the condition; none of it is the caller's. Through both real entry points the
# body carries a status and, at most, constants this template declared.
# ─────────────────────────────────────────────────────────────────────────────
_FAILING_REQUEST = {
    "input": "Please add a comment to it",
    "session_id": "containment-e2e-failure",
    "input_context": {"project": _PROJECT, "intent": "post_comment"},
}
_NODE_AUTHORED_MARKERS = ("error_log", "CallGitLabApiNode", "unresolved", "Traceback")


class TestWorkflowFailureIsContained:
    def test_inner_failure_publishes_no_node_authored_text(self, agent):
        ctx = InvocationContext(
            session_id=_FAILING_REQUEST["session_id"],
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        envelope = agent.invoke(_FAILING_REQUEST["input"], ctx=ctx, input_context=_FAILING_REQUEST["input_context"])
        assert envelope["status"] == "error"
        assert not envelope.get("output")
        assert _found(envelope, *_NODE_AUTHORED_MARKERS) == []

    def test_inner_failure_over_the_asgi_entry_point(self, client):
        resp = client.post("/invoke", json=_FAILING_REQUEST, headers={"Authorization": f"Bearer {_HTTP_TOKEN}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"
        assert not body.get("output")
        assert "error_log" not in body
        assert _found(body, *_NODE_AUTHORED_MARKERS) == []


# ─────────────────────────────────────────────────────────────────────────────
# Clean-path controls
#
# Without these the file passes on a gate that refuses everything, and the
# _found() probe above could be finding nothing because it works on nothing.
# ─────────────────────────────────────────────────────────────────────────────
class TestCleanPathControl:
    def test_the_same_request_still_returns_its_answer(self, agent):
        envelope = _invoke(agent, _PROJECT)
        assert envelope["status"] == "success"
        assert envelope["output"]
        assert envelope["output"]["resource_id"]
        assert envelope["resource_ref"].startswith(f"gitlab://{_PROJECT}/issues/")

    def test_the_probe_does_fire_on_a_successful_answer(self, agent):
        # Verify the verifier: the same probe every containment assertion uses
        # DOES find the evidence when the evidence is legitimately there.
        envelope = _invoke(agent, _PROJECT)
        assert _found(envelope, "Created GitLab issue", f"gitlab://{_PROJECT}") == [
            "Created GitLab issue",
            f"gitlab://{_PROJECT}",
        ]

    def test_the_gate_ran_on_the_clean_path_too(self, agent):
        assert "PostProcessNode" in _invoke(agent, _PROJECT)["node_history"]

    def test_the_asgi_clean_path_still_returns_its_answer(self, client):
        body = _post(client, _PROJECT).json()
        assert body["status"] == "success"
        assert body["output"]["resource_id"]


# ─────────────────────────────────────────────────────────────────────────────
# Reachability of the platform's own credential shapes
#
# Measured, not assumed: a credential the PLATFORM detector knows (AWS key id,
# Stripe key, a database connection string) never reaches post_process at all -
# InitializeNode echoes `input_context` back into its own delta and the
# framework's @final output gate raises there, several nodes earlier. That is why
# the union in _is_credential_like() cannot be falsified end-to-end and is
# pinned at unit level instead (tests/unit/test_post_process_node.py).
#
# The masking is the reason to keep the union rather than to skip it: it is the
# framework that closes these today, and a template gate narrower than the
# framework's is a bypass - the framework raises, and BaseNode.__call__ then
# discards the node's whole delta, the clearing included. These cases pin the
# OUTCOME (contained), not the mechanism.
# ─────────────────────────────────────────────────────────────────────────────
class TestPlatformKnownCredentialShapesAreAlsoContained:
    @pytest.mark.parametrize(
        "project",
        ["AKIA" + "A1B2C3D4E5F6G7H8", "sk_" + "live_" + "a" * 16],
        ids=["aws_key_shape", "stripe_key_shape"],
    )
    def test_no_payload_and_no_echo(self, agent, project):
        envelope = _invoke(agent, project)
        assert envelope["status"] == "error"
        assert not envelope.get("output")
        assert _found(envelope, project) == []
