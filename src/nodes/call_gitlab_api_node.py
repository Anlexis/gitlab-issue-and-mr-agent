"""AgentCore Platform v1.0 - inner workflow Step 4: CallGitLabApi (tool side-effect).

Performs the create-issue / open-merge-request / post-comment call against the
GitLab REST API v4 project endpoints via src/services/gitlab_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs. The node therefore stays ANONYMOUS.
  Credentials: the integration token is read via ctx.secrets.get("GITLAB_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored in
       state. v1 note: while the deterministic NETWORK-FREE stub transport is
       active a missing token is tolerated (a sentinel placeholder is used - it
       is never sent anywhere because no request leaves the process); with a
       LIVE transport injected, a missing token is a hard status=error - a real
       API is never called unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external system; HTTP 4xx/5xx surfaces as status=error +
       error_log (no silent pass). The log line carries the HTTP status only
       - never the API's own message, which is remote content - and a
       transport failure is reported by exception class, never by its text,
       which can quote the request URL.

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). GitLab settings (base_url) arrive as the JSON `gitlab_config` state
field - injected by the inner graph's _extra_initial_state() from the runtime
config section forwarded by GitLabWorkflowGraphNode._parent_config() - or via
the optional `config["configurable"]["gitlab"]` argument for direct invocation.
The client is constructed locally per call (no module-global mutation).

v1 scope: the GitLabClient is deliberately constructed here WITHOUT a live
transport - the deterministic NETWORK-FREE stub is the documented v1 delivery
scope (docs/02_design.md, "v1 Limitation - GitLab client"), so the deployed v1
graph performs no live GitLab operation and this node never fabricates one.
A live transport is a construction-time injection point at
src/services/gitlab_client.py, wired during deployment integration - not a v1
runtime capability.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.gitlab_client import GitLabApiError, GitLabClient

_SECRET_KEY = "GITLAB_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"

_VALID_INTENTS = ("create_issue", "open_merge_request", "post_comment")


class CallGitLabApiNode(FunctionNode):
    """Create an issue / open a merge request / post a comment via GitLab REST API v4."""

    # Trust: the external gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]", config: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        payload = from_json(state.get("gitlab_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallGitLabApiNode: missing gitlab_payload"],
            }

        intent = state.get("intent", "") or ""
        if intent not in _VALID_INTENTS:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallGitLabApiNode: unknown intent '{intent}'"],
            }

        # Settings: runtime config section from state (graph-injected), overridable via
        # an explicit config["configurable"]["gitlab"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("gitlab_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("gitlab") or {}
        settings.update(override)

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE v1 stub (documented limitation, docs/02).
        base_url = str(settings.get("base_url", "") or "").strip()
        client = GitLabClient(base_url=base_url) if base_url else GitLabClient()

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # v1 stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallGitLabApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        project = state.get("project_id", "") or ""
        if not project:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallGitLabApiNode: unresolved project - refusing to write " "to a guessed project"],
            }

        resource_title = state.get("resource_title", "") or ""

        try:
            if intent == "create_issue":
                if not payload.get("title"):
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallGitLabApiNode: no issue title extracted - cannot create issue"],
                    }
                resp = client.create_issue(project, payload, api_token) or {}
                resource_id = str(resp.get("iid", "") or "")
                resource_ref = f"gitlab://{project}/issues/{resource_id}" if resource_id else ""
                resource_title = resource_title or str(payload.get("title", ""))
            elif intent == "open_merge_request":
                missing = [k for k in ("source_branch", "target_branch", "title") if not payload.get(k)]
                if missing:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallGitLabApiNode: unresolved merge-request field(s) "
                            f"{', '.join(missing)} - branches and title are explicit-only"
                        ],
                    }
                resp = client.create_merge_request(project, payload, api_token) or {}
                resource_id = str(resp.get("iid", "") or "")
                resource_ref = f"gitlab://{project}/merge_requests/{resource_id}" if resource_id else ""
                resource_title = resource_title or str(payload.get("title", ""))
            else:  # post_comment
                kind = payload.get("target_kind", "")
                target_iid = payload.get("target_iid", "")
                body = payload.get("body", "")
                if not (kind and target_iid and body):
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallGitLabApiNode: unresolved comment target or empty body "
                            "(need an explicit `#N` / `!N` reference and comment text)"
                        ],
                    }
                resp = client.post_comment(project, kind, target_iid, {"body": body}, api_token) or {}
                resource_id = str(resp.get("id", "") or "")
                resource_ref = f"gitlab://{project}/{kind}s/{target_iid}#note_{resource_id}" if resource_id else ""
                resource_title = resource_title or (("!" if kind == "merge_request" else "#") + str(target_iid))
        except GitLabApiError as exc:
            # Closed-set entry: the HTTP status only. The exception's message
            # carries the GitLab error body - upstream content that can name
            # users or projects, or echo request fields - and capping or
            # stripping it would still leave arbitrary text in the log line,
            # so it is not echoed at all.
            status_code = exc.status_code
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallGitLabApiNode: GitLab API error {status_code}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Closed-set entry: the exception CLASS, never its message - a
            # transport error message can quote the URL it was sent to.
            failure_type = type(exc).__name__
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallGitLabApiNode: GitLab call failed: {failure_type}"],
            }

        # Audit the tool side-effect - intent + presence signals only,
        # never issue/comment content or credentials.
        emit_trace_event(
            "call_gitlab_api_complete",
            {
                "intent": intent,
                "has_resource_id": bool(resource_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "resource_id": resource_id,
            "resource_ref": resource_ref,
            "resource_title": resource_title,
            "project_id": project,
            "status": AgentStatus.SUCCESS.value,
        }
