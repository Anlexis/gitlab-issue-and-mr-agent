"""Input sanitizing helpers for CMN-C2-239.

Pure, stateless domain helpers (NOT framework gate methods), shared by the
outer backbone pre_process (instruction text + structured caller fields) so
that BOTH caller channels get the same treatment.

That sharing is the point: the platform's own input scan covers the
instruction text but not the structured caller channel, so a screen wired
only into the instruction path would leave caller-supplied text unscreened.
"""

from __future__ import annotations

import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Email addresses and access-token-like strings that might appear in a pasted
# request. Flagged and redacted before anything is logged or forwarded.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(
    r"\b(?:eyJ[A-Za-z0-9_-]{6,}|glpat-[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,})\b"
)
_REDACTION = "[REDACTED]"

DEFAULT_MAX_LENGTH = 4000

# ── Template-owned instruction-override screen ────────────────────────────────
# Request text is this agent's PRODUCT: callers legitimately write "ignore my
# previous comment", "override approved by the release manager", "acting as
# maintainer this week" — ordinary developer prose is full of directive verbs.
# A substring screen would refuse real work, so every alternative here is
# anchored on a full directive PHRASE aimed at the MODEL (an override verb
# plus a prompt/instruction noun, or a model role), or on a chat-template
# control token, which has no legitimate reading in a GitLab write request.
#
# This screen is the template's own guarantee, not the framework's: a platform
# input gate covers only user_input/validated_input (never the structured
# caller channel) and only where the hosting framework provides it — wherever
# it is absent the template would otherwise fail OPEN.
#
# The token class matters more than the phrases: stripping markup can DELETE a
# token like <|im_start|> and silently forward the directive residue, which is
# why callers of this screen check the text both as received and after the
# markup strip (see contains_instruction_override).
_INSTRUCTION_OVERRIDE_RE = re.compile(
    # Chat-template control tokens: <|im_start|>, <|system|>, [INST], <<SYS>>.
    r"<\|[a-z_]{2,32}\|>"
    r"|\[/?INST\]"
    r"|<</?SYS>>"
    # "ignore/disregard/forget ... previous/system instructions/prompt/rules".
    # The noun class is instruction-nouns ONLY — never "comment"/"message", so
    # "please ignore my previous comment" (real GitLab prose) passes.
    r"|\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+|your\s+|my\s+)*"
    r"(?:previous|prior|above|earlier|preceding|original|system)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions)\b"
    # bare "ignore all rules/instructions" (no temporal qualifier)
    r"|\b(?:ignore|disregard)\s+all\s+(?:rules|instructions)\b"
    # exfiltration: "reveal/print your system prompt / the hidden instructions".
    # "show the previous comment" stays ordinary GitLab talk.
    r"|\b(?:reveal|show|print|repeat|output|disclose|display|dump)\s+(?:me\s+)?"
    r"(?:your\s+(?:system\s+|initial\s+|hidden\s+)?(?:prompt|prompts|instructions)"
    r"|the\s+(?:system|initial|hidden)\s+(?:prompt|prompts|instructions|message))\b"
    # role reassignment: requires a MODEL role, so "you are now the release
    # manager" (a person's role) passes.
    r"|\byou\s+are\s+now\s+(?:a\s+|an\s+)?(?:different\s+|unrestricted\s+|new\s+|jailbroken\s+)?"
    r"(?:assistant|ai|chatbot|language\s+model|llm|dan)\b"
    # privileged-mode role-play: developer/admin/root + "mode". "act as a
    # release manager" or "acting as maintainer" never matches.
    r"|\bact\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?(?:a\s+|an\s+)?"
    r"(?:developer|admin|administrator|root|jailbroken|unrestricted)\s+mode\b"
    # rule-override: "override your/the instructions/safety/guardrails" —
    # "override approved by the maintainer" has no instruction-noun object.
    r"|\boverride\s+(?:your|the)\s+"
    r"(?:instruction|instructions|rule|rules|safety|guardrail|guardrails|restriction|restrictions)\b"
    # "new system prompt:" header form
    r"|\b(?:new|updated)\s+system\s+(?:prompt|instructions)\s*[:=]",
    re.IGNORECASE,
)


def contains_instruction_override(text: str) -> bool:
    """True when the text carries an instruction-override directive.

    Screens the text BOTH as received and after the markup strip: the raw form
    catches chat-template control tokens before ``sanitize_query`` would remove
    them (``<|im_start|>`` matches ``<[^>]+>``, so a strip-first pipeline would
    silently forward the directive residue), and the stripped form catches a
    directive spliced with markup (``[INST<b>]``) that would otherwise
    re-assemble inside the forwarded text.
    """
    if _INSTRUCTION_OVERRIDE_RE.search(text):
        return True
    stripped = _HTML_TAG_RE.sub("", text)
    return stripped != text and bool(_INSTRUCTION_OVERRIDE_RE.search(stripped))


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip HTML/markup tags (injection/markup guard) and cap length.

    Sanitizing is NOT refusal: callers must screen with
    ``contains_instruction_override`` BEFORE and not instead of this strip —
    a strip alone would remove a control token and forward its residue.
    """
    cleaned = _HTML_TAG_RE.sub("", query)
    return cleaned[:max_length]


def redact_sensitive(text: str) -> "tuple[str, list[str]]":
    """Flag-and-redact emails / token-like strings. Returns (redacted, flags).

    Flag-and-redact rather than reject on the free-text path: a pasted GitLab
    request legitimately carries contact details, so the hard rejections in
    this template are the empty/non-request guard and the instruction-override
    screen above. The flags name the CATEGORY found, never the value, so they
    are safe to log.
    """
    flags: list[str] = []
    redacted = text
    if _EMAIL_RE.search(redacted):
        flags.append("email")
        redacted = _EMAIL_RE.sub(_REDACTION, redacted)
    if _TOKEN_RE.search(redacted):
        flags.append("token")
        redacted = _TOKEN_RE.sub(_REDACTION, redacted)
    return redacted, flags
