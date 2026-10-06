"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: validate the caller's request (raw NL text + the
structured caller fields) and shape it for the inner GitLab workflow graph.
This node OWNS the caller-data contract: every field a caller can supply is
checked here, against explicit bounds, before any of it reaches the workflow.
Business rules (intent classification, entity extraction) live in the inner
graph.

Caller contract (`input_context`), every field optional:

    project / project_id / project_path   target project (namespace path or numeric id)
    intent                                create_issue | open_merge_request | post_comment
    title                                 issue / merge-request title, free text
    description                           issue / merge-request description, free text
    comment                               comment body, free text
    labels                                list of label names
    source_branch / target_branch         merge-request branch names
    target_kind                           issue | merge_request (comment target)
    target_iid                            target issue / merge-request iid for a comment

Rules applied to all of them:
  - values must be the declared TYPE. A number, boolean, mapping or list where
    a string is required is refused outright, never coerced: str(float("nan"))
    is "nan", which passes a shape check, so coercion would let a non-finite
    value into a write request.
  - values must match an explicit SHAPE and LENGTH bound; lists are entry-capped.
  - every caller-controlled NUMBER goes through a finite+bounded parser
    (bool / non-numeric / NaN / +-Infinity / out-of-range are refused - NaN
    comparisons are always False, so a non-finite value would otherwise fail
    OPEN on the exact decision it feeds).
  - instruction-override text (directives aimed at the MODEL: role
    reassignment, system-prompt manipulation, chat-template control tokens) is
    REFUSED, fail closed, on BOTH caller channels - the raw instruction text
    (as received AND after the markup strip) and every decoded string in
    input_context, keys included, at any depth. The screen is the template's
    own (src/services/security.py), never delegated to a hosting gate.
  - contact identifiers (email addresses, phone numbers, government / card
    numbers) in a structured caller field are REFUSED, naming the field and
    the category only. The detector's personal-NAME heuristic is deliberately
    excluded on this channel: it flags any two Title-Case words, and proper
    nouns are exactly what these fields legitimately carry ("Fix Login
    Timeout" is a title, not a person) - that exclusion is why the structured
    channel exists, and why free text remains subject to the platform's own
    masking while these fields are not.
  - a refusal names the FIELD and never echoes the offending value; a hostile
    field NAME is masked, never echoed either. Unknown fields are refused.
  - absent fields are simply absent: the workflow falls back to what it can
    read out of the instruction text.
"""

import json
import math
import re
from typing import Any, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import contains_instruction_override, redact_sensitive, sanitize_query

# GitLab project reference: numeric id, or a namespace path (group/sub/name).
# ASCII-only word chars keep the reference an inert identifier.
_PROJECT_SHAPE_RE = re.compile(r"^[A-Za-z0-9][\w.-]*(?:/[\w.-]+)*$", re.ASCII)
# Label: printable, conservative charset (scoped `a::b` labels included), <=50.
_LABEL_SHAPE_RE = re.compile(r"^[A-Za-z0-9_. :/-]{1,50}$")
# Branch name: word chars, dot, slash, dash; no "..", <=255.
_BRANCH_SHAPE_RE = re.compile(r"^[A-Za-z0-9_](?!.*\.\.)[A-Za-z0-9_./-]{0,254}$")

_MAX_PROJECT_LEN = 200
_MAX_TITLE_LEN = 255
_MAX_BODY_LEN = 65536
_MAX_LABELS = 20
_MAX_TARGET_IID = 1_000_000_000
_VALID_INTENTS = ("create_issue", "open_merge_request", "post_comment")
_VALID_TARGET_KINDS = ("issue", "merge_request")
# Alias priority preserved from the original contract: an explicit numeric
# project_id wins over a path-style alias when several are supplied.
_PROJECT_ALIASES = ("project_id", "project", "project_path")

# Contract field names that may be echoed into a refusal message. Any other
# input_context key is caller-controlled text, so its spot in the reported
# path shows a placeholder - a hostile field NAME is never echoed either.
_KNOWN_CONTEXT_FIELDS = frozenset(
    {
        "project",
        "project_id",
        "project_path",
        "intent",
        "title",
        "description",
        "comment",
        "labels",
        "source_branch",
        "target_branch",
        "target_kind",
        "target_iid",
    }
)

# Platform-injected metadata, not part of this template's write contract.
# run_agent_marketplace() seeds input_context={"conversation_history": history}
# UNCONDITIONALLY on every Marketplace invocation (shared/bootstrap/marketplace_app.py) -
# this is not caller-controlled contract data, so the fail-closed "unknown field"
# guard below must not refuse it, or every real invocation is rejected before intent
# classification ever runs. Its content is still covered by the instruction-override
# scan above, which walks the full input_context regardless of this allowlist.
_PLATFORM_METADATA_FIELDS = frozenset({"conversation_history"})


class CallerFieldError(ValueError):
    """A caller-supplied field failed its contract. Carries the field name only."""


def _find_instruction_override(value: object, path: str = "input_context") -> Optional[str]:
    """Depth-first scan of every decoded string in the mapping - keys included.

    Returns the path of the first string carrying an instruction-override
    directive, or None. The walk runs on the PARSED mapping, so JSON escaping
    (a ``\\u``-escaped payload) cannot smuggle a token past it, and it covers
    undeclared keys too: the screen must hold on what the caller SENT, not
    only on what the contract keeps. Path components outside the declared
    contract are masked, so the returned path is always safe to name in an
    error message.
    """
    if isinstance(value, str):
        return path if contains_instruction_override(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            safe_key = key if isinstance(key, str) and key in _KNOWN_CONTEXT_FIELDS else "<unrecognised-field>"
            key_path = f"{path}.{safe_key}"
            if isinstance(key, str) and contains_instruction_override(key):
                return key_path
            found = _find_instruction_override(item, key_path)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_instruction_override(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _require_text(value: object, field: str, max_len: int) -> str:
    """Return a bounded string, or raise naming the field (never the value).

    Rejects every non-string type, `bool` included, so numeric input cannot be
    stringified into something that satisfies a downstream shape check.
    """
    if not isinstance(value, str):
        raise CallerFieldError(f"'{field}' must be a string")
    text = value.strip()
    if not text:
        raise CallerFieldError(f"'{field}' must not be empty")
    if len(text) > max_len:
        raise CallerFieldError(f"'{field}' exceeds the {max_len}-character limit")
    return text


def _refuse_contact_identifiers(text: str, field: str) -> str:
    """Refuse contact identifiers in a structured caller field, else return it.

    Runs the platform detector but deliberately EXCLUDES the personal-name
    heuristic (it flags any two Title-Case words - the proper nouns these
    fields exist to carry). Everything else it finds - email addresses, phone
    numbers, government / card numbers - is refused, naming the field and the
    category, never the value.
    """
    findings = [f for f in detect_pii(text) if f["type"] not in ("name", "name_jp")]
    if findings:
        category = findings[0]["type"]
        raise CallerFieldError(f"'{field}' contains a contact identifier ({category})")
    return text


def _finite_int_in_range(value: object, field: str, lo: int, hi: int) -> int:
    """Parse a caller-controlled number: finite, integral, bounded - else refuse.

    bool is rejected explicitly (isinstance(True, int) is True); float input is
    accepted only when finite AND integral; strings are parsed then held to the
    same rule, so "NaN"/"Infinity" (which float() parses happily) are refused
    rather than flowing into a comparison that silently fails open.
    """
    if isinstance(value, bool):
        raise CallerFieldError(f"'{field}' must be an integer")
    if isinstance(value, int):
        number = float(value)
    elif isinstance(value, float):
        number = value
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise CallerFieldError(f"'{field}' must be an integer") from None
    else:
        raise CallerFieldError(f"'{field}' must be an integer")
    if not math.isfinite(number):
        raise CallerFieldError(f"'{field}' must be a finite number")
    if number != int(number):
        raise CallerFieldError(f"'{field}' must be an integer")
    if not (lo <= int(number) <= hi):
        raise CallerFieldError(f"'{field}' is out of range")
    return int(number)


def _validate_project(raw: object, field: str) -> str:
    """Validate a project reference (numeric id or namespace path)."""
    text = _require_text(raw, field, _MAX_PROJECT_LEN)
    if not _PROJECT_SHAPE_RE.match(text):
        raise CallerFieldError(f"'{field}' is not a valid project path or id")
    return text


def _validate_free_text(raw: object, field: str, max_len: int) -> str:
    """Bound, screen and redact a free-text field for the structured channel.

    These fields bypass the platform's free-text masking (that is the point of
    the channel - Title-Case titles must survive), so the template itself
    strips markup and refuses contact identifiers here.
    """
    cleaned = sanitize_query(_require_text(raw, field, max_len), max_length=max_len)
    if not cleaned.strip():
        raise CallerFieldError(f"'{field}' must not be empty")
    _refuse_contact_identifiers(cleaned, field)
    redacted, _flags = redact_sensitive(cleaned)
    return redacted


def _validate_label_list(raw: object, field: str) -> "list[str]":
    if not isinstance(raw, (list, tuple)):
        raise CallerFieldError(f"'{field}' must be a list")
    if len(raw) > _MAX_LABELS:
        raise CallerFieldError(f"'{field}' exceeds the {_MAX_LABELS}-entry limit")
    labels: list[str] = []
    for entry in raw:
        label = _require_text(entry, field, 50)
        if not _LABEL_SHAPE_RE.match(label):
            raise CallerFieldError(f"'{field}' contains an invalid label")
        labels.append(label)
    return labels


def _validate_branch(raw: object, field: str) -> str:
    text = _require_text(raw, field, 255)
    if not _BRANCH_SHAPE_RE.match(text):
        raise CallerFieldError(f"'{field}' is not a valid branch name")
    return text


def validate_caller_fields(input_context: object) -> "dict[str, Any]":
    """Validate the caller contract. Raises CallerFieldError on any breach.

    Unknown keys are refused (fail closed): a field this contract does not
    declare has no validated meaning downstream, and silently dropping it
    would let a typo change what a WRITE request does.
    """
    if input_context in (None, {}):
        return {}
    if not isinstance(input_context, dict):
        raise CallerFieldError("'input_context' must be a mapping")

    unknown = [
        k
        for k in input_context
        if not (isinstance(k, str) and (k in _KNOWN_CONTEXT_FIELDS or k in _PLATFORM_METADATA_FIELDS))
    ]
    if unknown:
        raise CallerFieldError("unrecognised field '<unrecognised-field>'")

    fields: dict[str, Any] = {}

    supplied = [k for k in _PROJECT_ALIASES if input_context.get(k) is not None]
    if supplied:
        fields["project"] = _validate_project(input_context[supplied[0]], supplied[0])

    if input_context.get("intent") is not None:
        intent = _require_text(input_context["intent"], "intent", 32)
        if intent not in _VALID_INTENTS:
            raise CallerFieldError("'intent' must be one of create_issue / open_merge_request / post_comment")
        fields["intent"] = intent

    if input_context.get("title") is not None:
        fields["title"] = _validate_free_text(input_context["title"], "title", _MAX_TITLE_LEN)
    if input_context.get("description") is not None:
        fields["description"] = _validate_free_text(input_context["description"], "description", _MAX_BODY_LEN)
    if input_context.get("comment") is not None:
        fields["comment"] = _validate_free_text(input_context["comment"], "comment", _MAX_BODY_LEN)

    if input_context.get("labels") is not None:
        fields["labels"] = _validate_label_list(input_context["labels"], "labels")

    if input_context.get("source_branch") is not None:
        fields["source_branch"] = _validate_branch(input_context["source_branch"], "source_branch")
    if input_context.get("target_branch") is not None:
        fields["target_branch"] = _validate_branch(input_context["target_branch"], "target_branch")

    if input_context.get("target_kind") is not None:
        kind = _require_text(input_context["target_kind"], "target_kind", 16)
        if kind not in _VALID_TARGET_KINDS:
            raise CallerFieldError("'target_kind' must be issue or merge_request")
        fields["target_kind"] = kind

    if input_context.get("target_iid") is not None:
        fields["target_iid"] = _finite_int_in_range(input_context["target_iid"], "target_iid", 1, _MAX_TARGET_IID)

    return fields


class PreProcessNode(FunctionNode):
    """Validate the caller contract and shape the request for the inner graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner GitLab call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # ── Instruction-override screen (template-owned, fail CLOSED) ────────
        # Runs on BOTH caller channels before anything is carried forward: a
        # refusal leaves no validated_input and no gitlab_request for any
        # downstream node. The screen lives in this node's own execute() path -
        # calling execute() directly still refuses, so the guarantee does not
        # depend on any hosting gate being present or configured on.
        if contains_instruction_override(user_input):
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": "user_input"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: request refused - instruction-override content in user_input"],
            }

        override_path = _find_instruction_override(input_context) if isinstance(input_context, (dict, list)) else None
        if override_path is not None:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": override_path},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: request refused - instruction-override content in {override_path}"],
            }

        try:
            caller_fields = validate_caller_fields(input_context)
        except CallerFieldError as exc:
            # Fail closed, naming the field only - the rejected value is never
            # echoed into the log.
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "caller_field", "detail": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: rejected caller input - {exc}"],
            }

        # Strip markup + cap length before JSON serialization.
        sanitized_input = sanitize_query(user_input.strip())

        project_hint = str(caller_fields.get("project", ""))
        validated_input = json.dumps({"text": sanitized_input, "project_hint": project_hint})

        # Audit the shaped request - field presence only, never the text.
        emit_trace_event(
            "pre_process_complete",
            {"has_project_hint": bool(project_hint), "caller_fields": sorted(caller_fields)},
            state,
        )

        return {
            "validated_input": validated_input,
            "project_hint": project_hint,
            # Stored as a JSON string (the state contract keeps every value
            # msgpack-safe); the graph node reads it back at the boundary.
            "gitlab_request": to_json(caller_fields) if caller_fields else None,
            "status": AgentStatus.SUCCESS.value,
        }
