# CMN-C2-239 - Unit tests: manifest + runtime-config sanity, and proof that
# every declared runtime value actually ARRIVES at its consumer (a declared
# setting with no live reader is a defect, not a default).

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_ROOT = pathlib.Path(__file__).parents[2]
_MANIFEST_PATH = _ROOT / "config" / "agent.yaml"
_RUNTIME_PATH = _ROOT / "config" / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text())


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text())


class TestManifest:
    """config/agent.yaml - the flat registry manifest (identity keys at ROOT level)."""

    def test_identity(self):
        data = _manifest()
        assert data["id"] == "CMN-C2-239"
        assert data["name"] == "GitLabIssueMRAgent"
        assert data["namespace"] == "cmn"
        assert data["category"] == "Cat 2"
        assert data["industry"] == "CMN"
        assert data["base_type"] == "ToolCallingAgent"
        assert data["enabled"] is True

    def test_entry_point_is_single_dotted_path(self):
        assert _manifest()["class"] == "src.graph.graph.GitLabIssueMRAgent"
        assert "module" not in _manifest()

    def test_generation_mode_is_deterministic(self):
        # v1 constructs no LLM client anywhere - the manifest must say so.
        assert _manifest()["generation_mode"] == "deterministic"

    def test_trust_level(self):
        assert _manifest()["required_trust_level"] == "VERIFIED_EXTERNAL"

    def test_requires_no_compile_time_secrets(self):
        # The GitLab token is read via ctx.secrets.get() (optional at compile
        # time: the network-free stub transport runs without it). Declaring it
        # under requires.secrets would make provisioning a hard compile-time
        # precondition - the declared list must therefore stay empty.
        data = _manifest()
        assert data["requires"]["secrets"] == []
        assert data["requires"]["extras"] == []


class TestRuntimeConfig:
    """config/config.yaml - runtime parameters, split from the static manifest."""

    def test_backbone_settings(self):
        data = _runtime()
        assert isinstance(data["max_retry"], int)
        assert isinstance(data["timeout_s"], int)

    def test_gitlab_integration_section(self):
        data = _runtime()
        assert data["gitlab"]["base_url"] == "https://gitlab.com/api/v4"


class TestDeclaredValuesArrive:
    """Every declared runtime value must reach its consumer."""

    def test_load_runtime_config_reads_the_shipped_file(self):
        from src.graph.graph import load_runtime_config

        loaded = load_runtime_config()
        assert loaded["max_retry"] == _runtime()["max_retry"]
        assert loaded["gitlab"]["base_url"] == "https://gitlab.com/api/v4"

    def test_backbone_consumes_max_retry(self):
        # The outer graph's retry routing reads self.config["max_retry"] - the
        # value must arrive through the constructor, not sit unread in a file.
        from src.graph.graph import GitLabIssueMRAgent, load_runtime_config

        agent = GitLabIssueMRAgent(config=load_runtime_config())
        assert agent.config["max_retry"] == _runtime()["max_retry"]
        assert agent.config["timeout_s"] == _runtime()["timeout_s"]

    def test_parent_config_forwards_gitlab_section_to_inner_graph(self):
        # The nested-graph boundary: the gitlab block declared in
        # config/config.yaml must arrive inside the inner graph's config.
        import json

        from src.graph.graph import GitLabWorkflowGraphNode

        node = GitLabWorkflowGraphNode()
        forwarded = node._parent_config()
        assert forwarded["configurable"]["gitlab"]["base_url"] == "https://gitlab.com/api/v4"

        subgraph = node.get_subgraph()
        seeded = subgraph._extra_initial_state()
        assert json.loads(seeded["gitlab_config"])["base_url"] == "https://gitlab.com/api/v4"

    def test_declared_base_url_reaches_the_rendered_output(self, tmp_path, monkeypatch):
        # The strongest arrival proof: rewrite the declared value on disk and
        # read the change off the rendered output. The stub transport derives
        # its synthetic resource id from the request URL, so a different
        # declared base_url MUST change the id in the confirmation - if it
        # does not, the declaration is dead.
        import src.graph.graph as graph_module
        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel

        def invoke_once():
            agent = graph_module.GitLabIssueMRAgent(config=graph_module.load_runtime_config())
            agent.compile()
            return agent.invoke(
                "create an issue about the login timeout",
                ctx=InvocationContext(session_id="config-arrival", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
                input_context={
                    "project": "example-group/webapp",
                    "intent": "create_issue",
                    "title": "config arrival probe",
                },
            )

        baseline = invoke_once()
        assert baseline["status"] == "success", f"baseline={baseline!r}"

        rewritten = tmp_path / "config.yaml"
        rewritten.write_text(
            _RUNTIME_PATH.read_text().replace("https://gitlab.com/api/v4", "https://gitlab.example.internal/api/v4")
        )
        monkeypatch.setattr(graph_module, "_RUNTIME_CONFIG_PATH", rewritten)
        changed = invoke_once()
        assert changed["status"] == "success", f"changed={changed!r}"

        assert baseline["resource_id"] != changed["resource_id"], (
            "rewriting gitlab.base_url in config/config.yaml did not change the "
            "rendered output - the declared value never reached the inner graph"
        )
