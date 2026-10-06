# CMN-C2-239 - Unit tests: CallGitLabApiNode (inner Step 4, tool side-effect).
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, trust gate unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's GitLabClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_gitlab_api_node import CallGitLabApiNode
from src.services.gitlab_client import GitLabApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_gitlab_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "gitlab_payload": to_json({"title": "login fails on safari"}),
        "intent": "create_issue",
        "project_id": "example-group/webapp",
        "resource_title": "login fails on safari",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-gitlab-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for GitLabClient: the call raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def create_issue(self, project, payload, api_token):
        raise GitLabApiError(403, "forbidden by project permissions")


class _FakeLiveClient:
    """Stands in for GitLabClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def create_issue(self, project, payload, api_token):
        _FakeLiveClient.captured = {"project": project, "payload": payload, "api_token": api_token}
        return {"iid": 4321, "title": str(payload.get("title", ""))}


class TestCallGitLabApiNode:
    def setup_method(self):
        self.node = CallGitLabApiNode()

    # -- success paths (deterministic, network-free v1 stub) ------------------

    def test_create_issue_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["resource_id"]
        assert result["resource_ref"] == f"gitlab://example-group/webapp/issues/{result['resource_id']}"
        assert result["resource_title"] == "login fails on safari"
        assert result["project_id"] == "example-group/webapp"

    def test_open_merge_request_success_via_default_v1_stub(self):
        state = _state(
            intent="open_merge_request",
            resource_title="login hotfix",
            gitlab_payload=to_json(
                {
                    "source_branch": "feature/login-fix",
                    "target_branch": "develop",
                    "title": "login hotfix",
                }
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["resource_id"]
        assert result["resource_ref"] == f"gitlab://example-group/webapp/merge_requests/{result['resource_id']}"

    def test_post_comment_success_via_default_v1_stub(self):
        state = _state(
            intent="post_comment",
            resource_title="#12",
            gitlab_payload=to_json({"target_kind": "issue", "target_iid": "12", "body": "fixed in develop"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["resource_id"]
        assert result["resource_ref"] == f"gitlab://example-group/webapp/issues/12#note_{result['resource_id']}"

    def test_gitlab_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `gitlab:` section as the JSON
        # gitlab_config state field; the stub transport still serves the call.
        state = _state(gitlab_config=to_json({"base_url": "https://gitlab.example.test/api/v4"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["resource_ref"].startswith("gitlab://example-group/webapp/issues/")

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"gitlab": {"base_url": "https://gitlab.example.test/api/v4"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["resource_id"]

    # -- guards / error paths -------------------------------------------------

    def test_missing_payload_errors(self):
        result = self.node(_state(gitlab_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_project"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_unresolved_project_errors(self):
        result = self.node(_state(project_id=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved project" in entry for entry in result["error_log"])

    def test_issue_without_title_errors(self):
        result = self.node(_state(gitlab_payload=to_json({"description": "no title extracted"})))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("no issue title" in entry for entry in result["error_log"])

    def test_mr_missing_branches_errors(self):
        state = _state(intent="open_merge_request", gitlab_payload=to_json({"title": "login hotfix"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        joined = "\n".join(result["error_log"])
        assert "source_branch" in joined
        assert "target_branch" in joined

    def test_comment_unresolved_target_errors(self):
        state = _state(
            intent="post_comment",
            gitlab_payload=to_json({"target_kind": "", "target_iid": "", "body": ""}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved comment target" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_gitlab_api_node.GitLabClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_api_error_body_is_not_echoed(self, monkeypatch):
        """A remote error body is upstream content - it can name users or
        projects, or echo request fields - so the log line carries the HTTP
        status only, never the message the API returned."""

        class _ShoutyClient(_FakeErrorClient):
            def create_issue(self, project, payload, api_token):
                raise GitLabApiError(403, "forbidden for user A. Tanaka on project secret/repo " + "x" * 300)

        monkeypatch.setattr("src.nodes.call_gitlab_api_node.GitLabClient", _ShoutyClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        joined = "\n".join(result["error_log"])
        assert "CallGitLabApiNode: GitLab API error 403" in joined
        assert "Tanaka" not in joined
        assert "secret/repo" not in joined
        assert "x" * 20 not in joined

    def test_transport_failure_is_reported_by_class_not_message(self, monkeypatch):
        """A transport exception's text can quote the URL it was sent to (and
        whatever rode in it); only the exception class is logged."""

        class _BrokenTransportClient(_FakeErrorClient):
            def create_issue(self, project, payload, api_token):
                raise ConnectionError("POST https://gitlab.internal/api/v4/projects/secret%2Frepo/issues refused")

        monkeypatch.setattr("src.nodes.call_gitlab_api_node.GitLabClient", _BrokenTransportClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        joined = "\n".join(result["error_log"])
        assert "CallGitLabApiNode: GitLab call failed: ConnectionError" in joined
        assert "gitlab.internal" not in joined
        assert "secret" not in joined

    # -- secret posture -------------------------------------------------------

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing GITLAB_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_gitlab_api_node.GitLabClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_gitlab_api_node.GitLabClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"GITLAB_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["project"] == "example-group/webapp"

    # -- audit ----------------------------------------------------------------

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_gitlab_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_gitlab_api_complete"]
        assert payload["intent"] == "create_issue"
        assert payload["has_resource_id"] is True
        assert payload["stub_transport"] is True
