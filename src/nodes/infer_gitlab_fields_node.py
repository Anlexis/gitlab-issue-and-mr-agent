"""AgentCore Platform v1.0 - inner workflow Step 3: InferGitLabFields.

Assembles a validated GitLab REST API v4 request body for the classified
intent. Field values come from the validated structured caller contract
(`gitlab_request` - title, description, comment, labels, source/target
branch, comment target) when supplied; anything the caller did not state
explicitly is extracted from the (redacted) request text by deterministic
regex. The project is taken only from an explicit mention in the text or the
caller-supplied project_hint - an unresolved project / branch / comment
target is left empty rather than invented (risk mitigation: never write to
the wrong project, branch, or thread; the executor surfaces the miss as
status=error). Deterministic v1 - no LLM (docs/02_design.md "v1
Implementation Note").

The structured fields are preferred for another reason besides precision: the
platform's free-text masking rewrites Title-Case runs in the request text, so
a proper-noun title extracted from text can arrive already masked. The
structured channel is validated by pre_process and NOT rewritten in flight,
so an explicit title survives verbatim into the API request.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

# A GitLab project reference: numeric id or namespace path (group/sub/name).
_PROJECT_SHAPE_RE = re.compile(r"^[A-Za-z0-9][\w.-]*(?:/[\w.-]+)*$", re.ASCII)
# Explicit project mention in the request text, EN or JA
# ("in project example-group/webapp" / "project: 1574" / "プロジェクト example-group/webapp").
_PROJECT_IN_TEXT_RE = re.compile(
    r"(?:in|for|to|on|under)\s+(?:the\s+)?project\s+[\"'`]?([A-Za-z0-9][\w.-]*(?:/[\w.-]+)*)"
    r"|(?:プロジェクト)\s*[:：]?\s*([A-Za-z0-9][\w.-]*(?:/[\w.-]+)*)",
    re.IGNORECASE,
)
# Quoted title: titled "Foo" / called 'Foo'. Curly quotes included so pasted
# rich text still parses.
_TITLE_QUOTED_RE = re.compile(r"(?:titled|called|named)\s+[\"'“]([^\"'”\n]+)[\"'”]", re.IGNORECASE)
# Prose description: `... with description: <rest of line>`.
_DESC_IN_TEXT_RE = re.compile(r"(?:with\s+)?description\s*[:：]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
# Branch pair: "from <src> into <tgt>" / "from branch <src> to <tgt>".
_BRANCH_PAIR_RE = re.compile(
    r"from\s+(?:branch\s+)?[\"'`]?([\w][\w./-]*)[\"'`]?\s+(?:into|to)\s+(?:branch\s+)?[\"'`]?([\w][\w./-]*)",
    re.IGNORECASE,
)
# Comment targets: merge request `!45` (checked first - more specific), issue `#123`.
_MR_REF_RE = re.compile(r"!(\d{1,7})\b")
_ISSUE_REF_RE = re.compile(r"#(\d{1,7})\b")
# Quoted comment body: saying "..." / that says '...'.
_SAYING_RE = re.compile(r"(?:saying|that\s+says)\s+[\"'“]([^\"'”\n]+)[\"'”]", re.IGNORECASE)
# "Key: value" request-field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs (literal, matching the fleet form).
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Recognized "Key:" spellings per field (lower-cased comparison).
_PROJECT_KEYS = ("project", "project id", "project path")
_TITLE_KEYS = ("title", "issue title", "mr title")
_DESC_KEYS = ("description", "desc")
_LABEL_KEYS = ("labels", "label")
_SOURCE_KEYS = ("source branch", "source")
_TARGET_KEYS = ("target branch", "target")
_BODY_KEYS = ("comment", "body", "note")


class InferGitLabFieldsNode(FunctionNode):
    """Extract entities and assemble the GitLab REST API v4 request body."""

    # Trust: inner domain node - derives fields from already-validated input;
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "") or ""
        project_hint = state.get("project_hint", "") or ""

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferGitLabFieldsNode: missing validated_input"],
            }
        if intent not in ("create_issue", "open_merge_request", "post_comment"):
            # Fail closed: never assemble a write payload for a missing/unknown
            # intent (every intent of this agent is a write - no safe default).
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"InferGitLabFieldsNode: missing or unknown intent '{intent}' - " "no write payload assembled"
                ],
            }

        # Validated structured caller fields (pre_process contract): an
        # explicit field always beats text extraction.
        request = dict(from_json(state.get("gitlab_request"), {}) or {})

        fields = self._parse_fields(text)
        project_id = str(request.get("project", "") or "") or self._resolve_project(text, project_hint, fields)
        title = str(request.get("title", "") or "") or self._resolve_title(text, fields)

        if intent == "create_issue":
            payload = self._build_issue_payload(title, text, fields, request)
            resource_title = title
        elif intent == "open_merge_request":
            payload = self._build_mr_payload(title, text, fields, request)
            resource_title = title
        else:  # post_comment
            payload = self._build_comment_payload(text, fields, request)
            kind, iid = payload.get("target_kind", ""), payload.get("target_iid", "")
            resource_title = ("!" if kind == "merge_request" else "#") + iid if iid else ""

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_gitlab_fields_complete",
            {"intent": intent, "has_project": bool(project_id), "n_fields": len(fields)},
            state,
        )

        return {
            "project_id": project_id,
            "resource_title": resource_title,
            "gitlab_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_project(self, text: str, project_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit project only: text mention > shape-valid hint > 'Project:' field. Never invented."""
        m = _PROJECT_IN_TEXT_RE.search(text)
        if m:
            return (m.group(1) or m.group(2) or "").rstrip(".,")
        hint = project_hint.strip()
        if hint and _PROJECT_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in _PROJECT_KEYS and _PROJECT_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_title(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _TITLE_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:200]
        for key, value in fields:
            if key.strip().lower() in _TITLE_KEYS:
                return value.strip()[:200]
        return ""

    def _first_field(self, fields: "list[tuple[str, str]]", keys: "tuple[str, ...]") -> str:
        for key, value in fields:
            if key.strip().lower() in keys:
                return value.strip()
        return ""

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] request fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields

    # -- payload assembly (GitLab REST API v4 shapes) --------------------------

    def _resolve_description(self, text: str, fields: "list[tuple[str, str]]", request: "dict[str, Any]") -> str:
        explicit = str(request.get("description", "") or "")
        if explicit:
            return explicit
        desc = self._first_field(fields, _DESC_KEYS)
        if desc:
            return desc
        m = _DESC_IN_TEXT_RE.search(text)
        return m.group(1).strip() if m else ""

    def _build_issue_payload(
        self, title: str, text: str, fields: "list[tuple[str, str]]", request: "dict[str, Any]"
    ) -> "dict[str, Any]":
        # POST /projects/:id/issues - documented body: title (required),
        # description, labels (comma-separated string).
        payload: dict[str, Any] = {"title": title}
        description = self._resolve_description(text, fields, request)
        if description:
            payload["description"] = description
        explicit_labels = [str(label) for label in (request.get("labels") or [])]
        labels = ",".join(explicit_labels) if explicit_labels else ""
        if not labels:
            raw = self._first_field(fields, _LABEL_KEYS)
            if raw:
                labels = ",".join(p.strip() for p in raw.split(",") if p.strip())
        if labels:
            payload["labels"] = labels
        return payload

    def _build_mr_payload(
        self, title: str, text: str, fields: "list[tuple[str, str]]", request: "dict[str, Any]"
    ) -> "dict[str, Any]":
        # POST /projects/:id/merge_requests - documented body: source_branch,
        # target_branch, title (all required), description. Branches are
        # explicit-only; unresolved values stay empty (executor errors).
        source = str(request.get("source_branch", "") or "") or self._first_field(fields, _SOURCE_KEYS)
        target = str(request.get("target_branch", "") or "") or self._first_field(fields, _TARGET_KEYS)
        m = _BRANCH_PAIR_RE.search(text)
        if m:
            source = source or m.group(1)
            target = target or m.group(2)
        payload: dict[str, Any] = {
            "source_branch": source,
            "target_branch": target,
            "title": title,
        }
        description = self._resolve_description(text, fields, request)
        if description:
            payload["description"] = description
        return payload

    def _build_comment_payload(
        self, text: str, fields: "list[tuple[str, str]]", request: "dict[str, Any]"
    ) -> "dict[str, Any]":
        # POST /projects/:id/{issues|merge_requests}/:iid/notes - documented
        # body: body (required). Target is explicit-only (`!N` / `#N` in the
        # text, or the validated target_kind/target_iid caller fields).
        kind = str(request.get("target_kind", "") or "")
        iid = str(request.get("target_iid", "") or "")
        if not (kind and iid):
            kind, iid = "", ""
            m = _MR_REF_RE.search(text)  # `!N` first - more specific than `#N`
            if m:
                kind, iid = "merge_request", m.group(1)
            else:
                m = _ISSUE_REF_RE.search(text)
                if m:
                    kind, iid = "issue", m.group(1)
        body = str(request.get("comment", "") or "") or self._first_field(fields, _BODY_KEYS)
        if not body:
            m = _SAYING_RE.search(text)
            body = m.group(1).strip() if m else ""
        return {"target_kind": kind, "target_iid": iid, "body": body}
