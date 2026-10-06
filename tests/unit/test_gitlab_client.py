# CMN-C2-239 - Unit tests: GitLabClient service (GitLab REST API v4 shape).
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.gitlab_client import GitLabApiError, GitLabClient


def test_create_issue_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 201, {"id": 104321, "iid": 4321, "title": body.get("title", "")}

    client = GitLabClient("https://gitlab.example.test/api/v4/", post=post)
    payload = {"title": "login fails on safari", "description": "happens on refresh"}
    resp = client.create_issue("example-group/webapp", payload, "tok123")
    assert resp["iid"] == 4321
    # Project references are URL-path-encoded in /projects/:id/.
    assert captured["url"] == "https://gitlab.example.test/api/v4/projects/example-group%2Fwebapp/issues"
    # GitLab REST API v4 auth: the per-call token travels in PRIVATE-TOKEN.
    assert captured["headers"]["PRIVATE-TOKEN"] == "tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"] == payload


def test_create_merge_request_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 201, {"id": 100007, "iid": 7, "title": body.get("title", "")}

    client = GitLabClient("https://gitlab.example.test/api/v4", post=post)
    payload = {"source_branch": "feature/login-fix", "target_branch": "develop", "title": "login hotfix"}
    resp = client.create_merge_request("example-group/webapp", payload, "tok")
    assert resp["iid"] == 7
    assert captured["url"] == "https://gitlab.example.test/api/v4/projects/example-group%2Fwebapp/merge_requests"
    assert captured["body"] == payload


def test_post_comment_issue_collection_url():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 201, {"id": 88, "body": body.get("body", "")}

    client = GitLabClient("https://gitlab.example.test/api/v4", post=post)
    resp = client.post_comment("1574", "issue", "12", {"body": "fixed in develop"}, "tok")
    assert resp["id"] == 88
    assert captured["url"] == "https://gitlab.example.test/api/v4/projects/1574/issues/12/notes"
    assert captured["body"] == {"body": "fixed in develop"}


def test_post_comment_merge_request_collection_url():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        return 201, {"id": 89, "body": body.get("body", "")}

    client = GitLabClient("https://gitlab.example.test/api/v4", post=post)
    client.post_comment("1574", "merge_request", "45", {"body": "verdict recorded"}, "tok")
    assert captured["url"] == "https://gitlab.example.test/api/v4/projects/1574/merge_requests/45/notes"


def test_post_comment_unknown_kind_raises_value_error():
    client = GitLabClient()
    with pytest.raises(ValueError):
        client.post_comment("1574", "snippet", "3", {"body": "x"}, "tok")


def test_non_2xx_raises_gitlab_api_error():
    def post(url, headers, body):
        return 400, {"message": {"title": ["can't be blank"]}}

    client = GitLabClient("https://gitlab.example.test/api/v4", post=post)
    with pytest.raises(GitLabApiError) as exc:
        client.create_issue("example-group/webapp", {}, "tok")
    assert exc.value.status_code == 400
    assert "title" in str(exc.value)


def test_default_stub_transport_issue_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = GitLabClient()
    assert client.uses_stub_transport is True
    resp = client.create_issue("example-group/webapp", {"title": "login fails on safari"}, "tok")
    assert resp.get("_stub") is True
    assert isinstance(resp["iid"], int)
    assert resp["iid"] > 0
    assert resp["title"] == "login fails on safari"


def test_default_stub_transport_note_shape():
    client = GitLabClient()
    resp = client.post_comment("example-group/webapp", "issue", "12", {"body": "fixed in develop"}, "tok")
    assert resp.get("_stub") is True
    assert isinstance(resp["id"], int)
    assert resp["body"] == "fixed in develop"


def test_default_stub_transport_is_deterministic():
    client = GitLabClient()
    first = client.create_issue("example-group/webapp", {"title": "login fails on safari"}, "tok")
    second = client.create_issue("example-group/webapp", {"title": "login fails on safari"}, "tok")
    assert first["iid"] == second["iid"]


def test_injected_transport_disables_stub_flag():
    client = GitLabClient(post=lambda url, headers, body: (201, {"iid": 1, "title": ""}))
    assert client.uses_stub_transport is False
