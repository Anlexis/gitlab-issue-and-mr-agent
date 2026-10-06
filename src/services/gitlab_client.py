"""AgentCore Platform v1.0 - GitLab REST API v4 client.

Service layer: a thin wrapper around the GitLab REST API v4 project write
endpoints (issues / merge requests / notes). Contains NO business logic, NO
routing, and NO credentials - the integration token is passed in per call by
the node (which reads it via ctx.secrets). This module imports no
framework/SDK internals - pure stdlib (import-isolation, PB-4).

v1 LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented GitLab response shapes (an issue / merge-request object with a
    synthetic ``iid``, a note object with a synthetic ``id`` - derived from the
    request) so the pipeline is runnable and testable without a live GitLab
    instance or the ``requests`` package - it does NOT perform a live GitLab
    call. A stub never impersonates a live call; the limitation is documented.
    The stub is the documented DELIVERY SCOPE of v1: the deployed v1 graph
    performs no live GitLab operation.

    To perform real GitLab calls, inject a live transport (requests-based
    ``post``) at construction time - a deployment-integration step, not a v1
    runtime capability; the method contracts and payload shapes are already
    GitLab REST API v4 exact, so no business-logic change is needed to go
    live. A live transport also requires a real integration token (see
    CallGitLabApiNode - the stub runs without one because no request ever
    leaves the process).
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://gitlab.com/api/v4"

# Comment targets map to their REST collection segment.
_NOTE_COLLECTIONS = {"issue": "issues", "merge_request": "merge_requests"}


class GitLabApiError(Exception):
    """Raised when the GitLab REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"GitLab API error {status_code}: {message}")


class GitLabClient:
    """GitLab REST API v4 project write client.

    Args:
        base_url: GitLab API base URL (default https://gitlab.com/api/v4).
        post: optional injected transport (tests or a live client). When none
            is injected, a deterministic NETWORK-FREE v1 stub is used (see the
            module docstring - it returns the documented shape without a live
            GitLab call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        post: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._post = post

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free v1 default)."""
        return self._post is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the GitLab REST API v4 auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "PRIVATE-TOKEN": api_token,
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: "dict[str, Any]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free v1 stub - returns the documented GitLab shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. See the module docstring for the v1
        limitation and how to inject a live transport.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        synthetic = int(digest[:6], 16) % 9000 + 1  # stable, non-zero
        if url.endswith("/notes"):
            # POST .../notes - documented note object (id, body).
            return 201, {
                "id": synthetic,
                "body": str(json_body.get("body", "")),
                "_stub": True,  # marks the network-free v1 stub response
            }
        # POST .../issues or .../merge_requests - documented resource object
        # subset (id, iid, title) echoing the request title.
        return 201, {
            "id": synthetic + 100000,
            "iid": synthetic,
            "title": str(json_body.get("title", "")),
            "_stub": True,  # marks the network-free v1 stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def _project_url(self, project: str, suffix: str) -> str:
        """Build /projects/:id/<suffix> with the path-encoded project reference."""
        encoded = urllib.parse.quote(str(project), safe="")
        return f"{self._base_url}/projects/{encoded}/{suffix}"

    def create_issue(self, project: str, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /projects/:id/issues - create an issue.

        ``payload`` is the documented body (``title`` required; ``description``,
        ``labels`` optional). Returns the parsed issue object (containing
        ``iid``). Raises GitLabApiError on a non-2xx status.
        """
        url = self._project_url(project, "issues")
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise GitLabApiError(status, _err_message(body))
        return body

    def create_merge_request(self, project: str, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /projects/:id/merge_requests - open a merge request.

        ``payload`` is the documented body (``source_branch``, ``target_branch``,
        ``title`` required; ``description`` optional). Returns the parsed
        merge-request object (containing ``iid``). Raises GitLabApiError on a
        non-2xx status.
        """
        url = self._project_url(project, "merge_requests")
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise GitLabApiError(status, _err_message(body))
        return body

    def post_comment(
        self, project: str, target_kind: str, target_iid: str, payload: "dict[str, Any]", api_token: str
    ) -> "dict[str, Any]":
        """POST /projects/:id/{issues|merge_requests}/:iid/notes - post a comment.

        ``target_kind`` is ``issue`` or ``merge_request``; ``payload`` is the
        documented body (``body`` required). Returns the parsed note object
        (containing ``id``). Raises GitLabApiError on a non-2xx status;
        ValueError on an unknown target kind (caller bug, not an API failure).
        """
        collection = _NOTE_COLLECTIONS.get(target_kind)
        if collection is None:
            raise ValueError(f"unknown comment target kind: {target_kind!r}")
        url = self._project_url(project, f"{collection}/{target_iid}/notes")
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise GitLabApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a GitLab error body."""
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error")
        if isinstance(msg, dict):
            return "; ".join(f"{k}: {v}" for k, v in msg.items())
        if msg:
            return str(msg)
    return str(body)
