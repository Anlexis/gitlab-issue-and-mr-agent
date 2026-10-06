# CMN-C2-239 - Unit tests: InferGitLabFieldsNode (inner Step 3).
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.
# Positive payloads are proper-noun-free: the framework mask rewrites Title-Case
# bigrams in validated_input, so text-path titles / bodies stay lower-case
# (explicit caller-contract fields are the unmasked channel - tested below).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_gitlab_fields_node import InferGitLabFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_gitlab_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "create_issue", project_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "project_hint": project_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferGitLabFieldsNode:
    def setup_method(self):
        self.node = InferGitLabFieldsNode()

    # -- create_issue ---------------------------------------------------------

    def test_issue_project_title_description_from_text(self):
        text = 'Create an issue titled "login fails on safari" in project example-group/webapp with description: happens on the 17.4 build only'
        result = self.node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["project_id"] == "example-group/webapp"
        assert result["resource_title"] == "login fails on safari"
        # State contract: gitlab_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["gitlab_payload"], str)
        assert from_json(result["gitlab_payload"], {}) == {
            "title": "login fails on safari",
            "description": "happens on the 17.4 build only",
        }

    def test_issue_fields_from_kv_lines(self):
        text = "File a bug\nProject: example-group/webapp\nTitle: cart total wrong\nDescription: totals drift on refresh\nLabels: bug, checkout"
        result = self.node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["project_id"] == "example-group/webapp"
        payload = from_json(result["gitlab_payload"], {})
        assert payload["title"] == "cart total wrong"
        assert payload["description"] == "totals drift on refresh"
        assert payload["labels"] == "bug,checkout"

    # -- project resolution (explicit-only, never invented) -------------------

    def test_shape_valid_hint_used_when_text_silent(self):
        result = self.node(_state('Create an issue titled "broken cart flow"', project_hint="example-group/shop"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["project_id"] == "example-group/shop"

    def test_text_mention_beats_hint(self):
        text = 'Create an issue titled "broken cart flow" in project example-group/webapp'
        result = self.node(_state(text, project_hint="other/project"))
        assert result["project_id"] == "example-group/webapp"

    def test_malformed_hint_left_unresolved(self):
        result = self.node(_state('Create an issue titled "broken cart flow"', project_hint="not a project!"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["project_id"] == ""  # left empty - never invented

    # -- open_merge_request ---------------------------------------------------

    def test_mr_branch_pair_from_text(self):
        text = 'Open a merge request from feature/login-fix into develop titled "login hotfix"'
        result = self.node(_state(text, intent="open_merge_request"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["gitlab_payload"], {})
        assert payload["source_branch"] == "feature/login-fix"
        assert payload["target_branch"] == "develop"
        assert payload["title"] == "login hotfix"

    def test_mr_fields_from_kv_lines(self):
        text = "Open an MR\nProject: example-group/webapp\nSource branch: feature/sync-a\nTarget branch: develop\nMR title: sync the a-side work"
        result = self.node(_state(text, intent="open_merge_request"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["project_id"] == "example-group/webapp"
        payload = from_json(result["gitlab_payload"], {})
        assert payload["source_branch"] == "feature/sync-a"
        assert payload["target_branch"] == "develop"
        assert payload["title"] == "sync the a-side work"

    def test_mr_unresolved_branches_left_empty(self):
        result = self.node(_state('Open a merge request titled "login hotfix"', intent="open_merge_request"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["gitlab_payload"], {})
        assert payload["source_branch"] == ""  # explicit-only - executor errors
        assert payload["target_branch"] == ""

    # -- post_comment ---------------------------------------------------------

    def test_comment_mr_ref_more_specific_than_issue_ref(self):
        text = 'Comment on merge request !45 in project example-group/webapp saying "verdict recorded, merging"'
        result = self.node(_state(text, intent="post_comment"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["gitlab_payload"], {})
        assert payload["target_kind"] == "merge_request"
        assert payload["target_iid"] == "45"
        assert payload["body"] == "verdict recorded, merging"
        assert result["resource_title"] == "!45"

    def test_comment_issue_ref(self):
        text = 'Reply to #12 in project example-group/webapp saying "fixed in develop"'
        result = self.node(_state(text, intent="post_comment"))
        payload = from_json(result["gitlab_payload"], {})
        assert payload["target_kind"] == "issue"
        assert payload["target_iid"] == "12"
        assert payload["body"] == "fixed in develop"
        assert result["resource_title"] == "#12"

    def test_comment_body_from_kv_line(self):
        text = "Add a note on #7 in project example-group/webapp\nComment: deployment verified"
        result = self.node(_state(text, intent="post_comment"))
        payload = from_json(result["gitlab_payload"], {})
        assert payload["target_kind"] == "issue"
        assert payload["target_iid"] == "7"
        assert payload["body"] == "deployment verified"

    def test_comment_unresolved_target_left_empty(self):
        result = self.node(_state('Comment saying "well done" on the last change', intent="post_comment"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["gitlab_payload"], {})
        assert payload["target_kind"] == ""  # explicit `#N` / `!N` only
        assert payload["target_iid"] == ""

    # -- guards ---------------------------------------------------------------

    # -- validated caller contract beats text extraction ----------------------

    def test_contract_title_description_labels_beat_text(self):
        from src.schemas.state import to_json

        state = _state('Create an issue titled "text title" in project a/b')
        state["gitlab_request"] = to_json(
            {"title": "Fix Login Timeout Bug", "description": "Seen On Safari", "labels": ["bug", "ops"]}
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["gitlab_payload"], {})
        # Proper-noun fields arrive verbatim from the validated contract.
        assert payload["title"] == "Fix Login Timeout Bug"
        assert payload["description"] == "Seen On Safari"
        assert payload["labels"] == "bug,ops"
        assert result["resource_title"] == "Fix Login Timeout Bug"

    def test_contract_project_beats_hint_and_text(self):
        from src.schemas.state import to_json

        state = _state("create an issue titled 'x' in project text/project", project_hint="hint/project")
        state["gitlab_request"] = to_json({"project": "contract/project", "title": "x"})
        result = self.node(state)
        assert result["project_id"] == "contract/project"

    def test_contract_branches_beat_text(self):
        from src.schemas.state import to_json

        state = _state("open a merge request from text-src into text-tgt titled 'y'", intent="open_merge_request")
        state["gitlab_request"] = to_json({"source_branch": "feature/retry-fix", "target_branch": "main", "title": "y"})
        result = self.node(state)
        payload = from_json(result["gitlab_payload"], {})
        assert payload["source_branch"] == "feature/retry-fix"
        assert payload["target_branch"] == "main"

    def test_contract_comment_target_beats_text(self):
        from src.schemas.state import to_json

        state = _state("add a note on !7 saying 'text body'", intent="post_comment")
        state["gitlab_request"] = to_json({"target_kind": "issue", "target_iid": 12345, "comment": "contract body"})
        result = self.node(state)
        payload = from_json(result["gitlab_payload"], {})
        assert payload["target_kind"] == "issue"
        assert payload["target_iid"] == "12345"
        assert payload["body"] == "contract body"
        assert result["resource_title"] == "#12345"

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_unknown_intent_fails_closed(self):
        result = self.node(_state("Create an issue in project example-group/webapp", intent="delete_project"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_missing_intent_fails_closed(self):
        result = self.node(_state("Create an issue in project example-group/webapp", intent=""))
        assert result["status"] == AgentStatus.ERROR.value

    def test_audit_emits_field_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.infer_gitlab_fields_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state('Create an issue titled "login fails on safari" in project example-group/webapp'))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - signals only, no content.
        payload = payloads["infer_gitlab_fields_complete"]
        assert payload["intent"] == "create_issue"
        assert payload["has_project"] is True
        assert isinstance(payload["n_fields"], int)
