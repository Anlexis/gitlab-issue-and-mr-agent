# Template Design Specification — CMN-C2-239 GitLab Issue & MR Agent

## Position in AgentCore Architecture

- **Agent Class**: `GitLabIssueMRAgent` (`src/graph/graph.py`)
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
- **Category**: Cat 2 (multi-step domain workflow, ToolCallingAgent). Outer
  `AgentBaseGraph` 5-node backbone; the domain pipeline is encapsulated in a
  `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
  (`src/graph/domain_workflow_graph.py`).
- **Agent Type**: ToolCallingAgent — classify intent -> extract GitLab
  fields -> build a GitLab REST API v4 request -> call the tool -> format the
  confirmation. No RAG retrieval, no autonomous ReAct loop.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — msgpack incompatible)
  - Node: framework base-class inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
    not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | own the caller-data contract: validate every `input_context` field (type/shape/bounds, finite numbers, entry caps), refuse instruction-override content on BOTH channels (raw + post-strip, parsed context incl. keys), refuse contact identifiers in structured fields; serialize NL text into `validated_input` (JSON) and the validated fields into `gitlab_request` | user_input, input_context | validated_input, project_hint, gitlab_request | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner GitLab workflow subgraph; stash `gitlab_request` on the context bridge; contain inner errors (domain messages, never a traceback) | validated_input, gitlab_request | result, intent, project_id, resource_id, resource_ref, resource_title, confirmation, gitlab_payload | GraphNode (caller ctx forwarded unchanged) | GitLabWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output` — scalar evidence fields only (the raw `gitlab_payload` request dict is never surfaced); module-level `_security_gate_output()` scan (recursive over nested values and keys) | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

Inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline
steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------------|
| validate_input | 1 ValidateInput | empty/non-request guard; deterministic (regex) flag-and-redact of email/token-like strings before logging | validated_input, project_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | explicit validated `intent` from the caller contract wins; otherwise deterministic keyword classification -> create_issue / open_merge_request / post_comment; every intent of this agent is a WRITE, so a no-signal / low-confidence request is a hard `status=error` — never a guessed write | intent | ANONYMOUS |
| infer_gitlab_fields | 3 InferGitLabFields | assemble the GitLab REST API v4 request body per intent from the validated caller contract first (title, description, comment, labels, source/target branch, comment target), text extraction (mentions + "Key: value" lines) as the fallback; an unresolved project / branch / comment target is left empty (never invented) | project_id, resource_title, gitlab_payload | ANONYMOUS |
| call_gitlab_api | 4 CallGitLabApi | POST /projects/:id/issues (create_issue) / POST /projects/:id/merge_requests (open_merge_request) / POST /projects/:id/{issues\|merge_requests}/:iid/notes (post_comment) via `GitLabClient`; token via ctx.secrets; 4xx/5xx -> status=error | resource_id, resource_ref, resource_title, project_id | ANONYMOUS |
| confirm | 5 Confirm | format intent + resource id + reference into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / GitLabWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_gitlab_fields
              -> call_gitlab_api -> confirm -> END
```

Structured parameters travel on two channels:

- the NL text as a JSON string — `pre_process` serializes `{"text",
  "project_hint"}` into `validated_input`,
  `GitLabWorkflowGraphNode.extract_input()` hands that JSON to the subgraph,
  and the first inner node (`validate_input`) parses it back;
- the validated caller contract as the JSON `gitlab_request` field — the
  graph-node boundary does not forward `input_context`, so
  `extract_input()` stashes it on a ContextVar bridge
  (`src/graph/context_bridge.py`) and the inner graph's
  `_extra_initial_state()` seeds it into inner State. The bridge channel is
  NOT rewritten by the platform's free-text masking, which rewrites
  Title-Case runs in `user_input`/`validated_input` at every node boundary —
  a proper-noun title supplied via `input_context` therefore survives
  verbatim into the API request, while the same title carried in free text
  arrives masked. Callers with exact titles/labels should use
  `input_context`.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (state contract —
fields are absent until their producer node writes them). Dict/list payloads
are stored as JSON strings (`Optional[str]`) via the module helpers
`to_json` / `from_json`, used by every producer and consumer.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| project_hint | NotRequired[str] | caller-supplied project path/id, validated by pre_process; never inferred | pre_process / validate_input |
| gitlab_request | NotRequired[Optional[str]] | JSON — validated structured caller contract (project / intent / title / description / comment / labels / source_branch / target_branch / target_kind / target_iid); crosses the graph boundary via the context bridge | pre_process |
| project_id | NotRequired[str] | resolved GitLab project path or numeric id (explicit in the contract, the text, or the hint — never invented) | infer_gitlab_fields |
| redaction_flags | NotRequired[Optional[str]] | JSON list of pattern categories redacted before logging | validate_input |
| resource_title | NotRequired[str] | issue / merge-request title (or comment target label) | infer_gitlab_fields / call_gitlab_api |
| gitlab_payload | NotRequired[Optional[str]] | JSON — assembled GitLab REST API v4 request body (msgpack-safe: stored as a JSON string via `to_json`/`from_json`) | infer_gitlab_fields |
| gitlab_config | NotRequired[Optional[str]] | JSON — runtime `gitlab:` section (config/config.yaml) forwarded by `_parent_config()` and injected via the inner graph's `_extra_initial_state()` | inner graph |
| resource_id | NotRequired[str] | iid / note id returned by GitLab | call_gitlab_api |
| resource_ref | NotRequired[str] | human-readable resource reference (`gitlab://<project>/issues/<iid>` style) | call_gitlab_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from
`AgentState` and are **not** re-declared.

**State Constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the GitLab token is accessed via `ctx.secrets`.
- InvocationContext read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration (split manifest + runtime file, nested Cat-2 forwarding)

`config/agent.yaml` is the static registry manifest (identity keys at ROOT
level — id, name, namespace, class, trust level, requires). Every runtime
value lives in `config/config.yaml`:

- `max_retry` / `timeout_s` — consumed by the OUTER backbone. The standalone
  entry point passes the loaded file to the agent constructor
  (`GitLabIssueMRAgent(config=load_runtime_config())`) so these values
  actually reach the retry routing; an agent constructed without config would
  silently run on framework defaults.
- `gitlab:` (base_url) — consumed by the INNER graph. Nodes take **no
  constructor arguments** (SDK v1 nodes are no-arg; configuration never rides
  on node instances), so `GitLabWorkflowGraphNode._parent_config()` loads
  `config/config.yaml` and forwards the `gitlab:` section under
  `config["configurable"]` — never `{}` while the file declares one (an empty
  `_parent_config()` silently disconnects every declared setting). The inner
  graph's `_extra_initial_state()` then injects the section into State as a
  JSON string (`gitlab_config`), where
  `CallGitLabApiNode.execute(state, config=None)` reads it (an explicit
  `config["configurable"]["gitlab"]` override is also honoured for
  direct/unit invocation).

The backbone keys are deliberately NOT forwarded to the inner graph (no inner
node reads them), and nothing else in the runtime file is declared without a
live reader — the config test suite proves each declared value arrives at its
consumer, including an on-disk rewrite of `gitlab.base_url` that must change
the rendered output.

## Structured Output (`get_output()` override)

CMN-C2-239 is a **structured-product** template: callers act on the returned
issue / MR / note identifiers, not only on rendered text.
`GitLabIssueMRAgent.get_output()` therefore **extends** `super().get_output()`
(the base envelope — output/status/trace_id/correlation_id/node_history — is
never replaced) with the structured action keys `intent`, `project_id`,
`resource_id`, `resource_ref`, `resource_title`, `confirmation`. Structured
keys are surfaced **only on SUCCESS**; before surfacing they are re-scanned by
the module-level output gate and any violation fails closed (structured keys
suppressed, `status=error`). On any non-SUCCESS status the structured keys are
withheld (an errored or gated response never exposes partial resource/payload
data).

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner
  domain node — **including the write-capable `CallGitLabApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary
  is therefore enforced exactly once, at `pre_process`. Agent-level default trust
  `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`. `src/api/server.py`
  enforces the standalone entry-point Bearer-token auth boundary
  (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation) and coarse transport
  caps on `input_context` (16 keys / 256 KB).
- **Input screens (template-owned)** — `PreProcessNode` owns the caller
  contract and fails CLOSED, with the guarantees enforced in its own
  `execute()` path (they hold with no hosting gate in front):
  - *Instruction-override refusal* on BOTH channels: the raw text (as
    received AND after the markup strip — a strip alone would delete a
    chat-template token like `<|im_start|>` and forward the directive
    residue) and every decoded string in `input_context`, keys included, at
    any depth. The screen is phrase-anchored (directive verb + instruction
    noun, or a model role, or a chat-template control token) so ordinary
    developer prose is unaffected.
  - *Field validation*: every `input_context` field type-, shape- and
    length-bounded; lists entry-capped; every caller-controlled number
    through a finite+bounded parser (bool / non-numeric / NaN / +-Infinity /
    out-of-range refused — NaN comparisons are always False, so a non-finite
    value would otherwise fail OPEN); unknown fields refused; refusals name
    the field, never the value, and a hostile field NAME is masked.
  - *Contact-identifier refusal* on structured fields (emails, phone
    numbers, card/government numbers via the platform detector) — the
    detector's personal-NAME heuristic is deliberately excluded on this
    channel (it flags any two Title-Case words; proper-noun titles are what
    these fields legitimately carry), which is documented in the Data Flow
    section as the channel's purpose.
  - `ValidateInputNode.execute()` additionally runs a deterministic (regex,
    NOT LLM) flag-and-redact for email addresses and access-token-like
    strings (`eyJ...`, `glpat-...`, `secret_...`, `sk-...`) on the free-text
    path before any logging — a pasted request legitimately carries contact
    details, so the text path redacts rather than rejects; the framework PII
    mask in `BaseNode.__call__` additionally masks emails/phones/names in
    `user_input`/`validated_input`.
- **Secrets + output gate** — the integration token is read via
  `ctx.secrets.get("GITLAB_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. It is deliberately NOT listed
  under `config/agent.yaml requires.secrets`: the node reads it with
  `ctx.secrets.get()` (optional — the network-free stub transport runs
  without it), and declaring a compile-time secret would make provisioning a
  hard precondition of every deployment. A missing token is tolerated **only**
  while the deterministic network-free stub transport is active (no live call is
  made); with a live transport injected, a missing token is a hard
  `status=error`. The domain output gate is the **module-level**
  `_security_gate_output()` in `src/nodes/post_process_node.py`, called from
  `PostProcessNode.execute()`: it blocks any SUCCESS response that lacks
  resource evidence (resource_id/resource_ref) and any credential-shaped
  string in `formatted_output` — scanned **recursively** across nested
  dict/list values and keys, so a surfaced container can never smuggle a
  credential past the gate. The caller-facing output itself is contained: the
  raw `gitlab_payload` request dict is never surfaced — only the scalar
  evidence fields (resource_id / resource_ref / resource_title / intent /
  confirmation) pass through. The same module-level gate re-scans the
  structured keys surfaced by `GitLabIssueMRAgent.get_output()` (structured
  product — fail-closed). No node defines
  `_extra_security_gate_input/_output` instance methods (framework hooks are
  @final / auto-wrapped — domain checks live inline or in module-level helpers).
- **Error containment** — an error response must never disclose that a GitLab
  write happened or which record it touched. `resource_id` / `resource_ref`
  **are** that evidence (the gate above refuses a SUCCESS that lacks them), so
  on *every* `PostProcessNode` error return the node (a) ships the contained
  envelope from `_error_output()` — the error `status` + one constant `reason`
  code (a closed set: never `error_log` or the gate's violation entries, which
  are node-authored text), and
  deliberately **non-empty**, because `AgentBaseGraph.get_output()` projects
  `formatted_output or result` with no status check and a falsy
  `formatted_output` re-opens that fallback onto whatever survived — and
  (b) **blanks** `resource_id` / `resource_ref` / `resource_title` /
  `confirmation` / `gitlab_payload` / `result` in State, so a checkpoint or a
  downstream reader cannot pick them up either. Omitting them from one
  envelope while leaving them in State is containment in form only.
  `GitLabIssueMRAgent.get_output()` closes the same hole one layer up: on any
  non-SUCCESS status the envelope's `output` slot is emptied rather than
  passed through from `formatted_output or result` — which also covers the
  error paths where `post_process` never runs at all (an errored `main`
  routes straight to `finalize`). When its re-scan refuses a response the
  envelope carries a constant `reason` code only; `error_log` and the
  violation entries stay internal.
- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its
  SUCCESS path (intent / presence signals only — never request text, issue or
  comment content, or credentials). `__call__()` is never overridden;
  `_invoke_impl` is never defined on any node. Event names (documented for
  operations):

  | Node | Audit event |
  |------|-----------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_gitlab_fields | `infer_gitlab_fields_complete` |
  | call_gitlab_api | `call_gitlab_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete` |

- **Error containment** — `GitLabWorkflowGraphNode` uses the non-raising
  error strategy: an inner-graph rejection (a caller mistake — unsupported
  operation, unresolved project, missing title) returns a structured
  error envelope carrying the inner node's own field-naming message. The
  default re-raise strategy would record a full Python traceback (runtime
  file paths) in `error_log` and drop the domain message; an UNEXPECTED
  exception type is contained with a generic message, never the exception's
  own wording.

## v1 Implementation Note — LLM synthesis

v1 is fully deterministic: intent classification (`ClassifyIntentNode`) uses a
keyword heuristic and field inference (`InferGitLabFieldsNode`) uses regex/
line-structure extraction, so the template runs and tests without a live LLM.
**No LLM client is constructed anywhere in v1** and no `system_prompt` is read
(no dead config). LLM-backed synthesis (richer intent classification,
free-text-to-field mapping, generated issue/MR descriptions) is a documented
follow-up: adding it means declaring an `llm:` block in `config/config.yaml`,
forwarding it in `_parent_config()` alongside the `gitlab:` section, and
constructing the client in the classifier — additive work that requires no
graph-shape change. No dead `llm` declaration or reader ships in v1.

## v1 Limitation — GitLab client (documented)

`src/services/gitlab_client.py` is a thin service layer (injectable
transport, `GitLabApiError`, per-call token, no framework imports) that ships
a **deterministic, network-free v1 stub** as its default transport: it returns
the documented GitLab REST API v4 response shapes (an issue / merge-request
object with a synthetic `iid`, a note object with a synthetic `id` — derived
from the request) so the pipeline is runnable and testable without a live
GitLab instance or the `requests` package. It does **not** perform a live
GitLab call. The policy is: never fake a live call — document the limitation.

The stub is the **documented delivery scope of v1**: the deployed v1 graph
performs no live GitLab operation, and `CallGitLabApiNode` deliberately never
constructs a live transport. The live transport is a **construction-time
injection point for deployment integration** — inject a real `post` transport
at `GitLabClient` construction when wiring the deployment — not a v1 runtime
capability. The method contracts and payload shapes are already GitLab REST
API v4 exact, so going live requires no business-logic change. (The stub also
runs without a live credential — see Security Design above; a live transport
requires `GITLAB_TOKEN`.)

## Framework Utilization

### Shared Components Used
- [x] InvocationContext — read in `CallGitLabApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallGitLabApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("GITLAB_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py` (optional at compile time — see Security Design)
- [x] Audit `emit_trace_event()` — one positional call per node on the SUCCESS path; framework lifecycle events (node_start/node_complete/node_error/s1_denied) NOT re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `GitLabWorkflowGraph` (`BaseGraph`) via `GitLabWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `GitLabWorkflowGraphNode._parent_config()` loads
  `config/config.yaml` and forwards the `gitlab:` section under
  `config["configurable"]` to the subgraph.
- **Caller-context forwarding**: the validated contract crosses via the
  ContextVar bridge (`src/graph/context_bridge.py`) — `extract_input()`
  stashes, `_extra_initial_state()` seeds (the graph-node boundary does not
  forward `input_context`).
- **Error strategy**: non-raising ("graceful") — an inner rejection returns a
  structured envelope with the inner node's own message via
  `on_subgraph_error()`; per-step `status=error` + `error_log` for
  API/validation failures (no silent pass, no traceback to the caller).

## Import Isolation Confirmation
- [x] Template imports `framework/` and `shared/` only; no platform-internal SDK import anywhere
- [x] `src/services/gitlab_client.py` and `src/services/security.py` have no
      framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat Cat 1 (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat — `gate-composition`; 5 domain steps live in the inner graph |
| LLM dependency | LLM client in v1 | deterministic v1, LLM as documented follow-up | deterministic v1 | template runs/tests without a live LLM; no dead prompt/config reads; see "v1 Implementation Note — LLM synthesis" |
| GitLab client | live `requests` call | injectable transport + documented v1 stub default | injectable + v1 stub default | no faked live network calls; document the limitation; go-live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + runtime-config forwarding via `_parent_config()` -> `configurable` -> state | no-arg nodes | SDK v1 nodes are no-arg (ctor args TypeError at graph build); config/config.yaml stays the single runtime-config source |
| Write target | infer project / branches / comment target from NL freely | explicit mention, validated caller field, or hint only; unresolved left empty | explicit only | never write to the wrong project, branch, or thread; unresolved target -> status=error, not invented |
| Unknown intent | default to one of the write intents | fail closed (status=error, no call) | fail closed | every intent of this agent is a WRITE — low-confidence classification must never guess a write (no read-only fallback exists) |
| Structured output | rendered text only | `get_output()` override extending the base envelope | `get_output()` override | callers act on issue/MR/note identifiers; structured keys SUCCESS-only, re-scanned by the output gate fail-closed |
| Inner-error surface | re-raise as SubgraphError | non-raising envelope via on_subgraph_error | non-raising envelope | a caller typo is a domain outcome, not a bug: the envelope carries the inner node's field-naming message; the re-raise path records a Python traceback (runtime paths) in error_log and drops the domain message |
| Proper-noun fields | free-text extraction only | validated structured channel via input_context + context bridge | structured channel first | the platform masks Title-Case runs in `user_input`/`validated_input` at every node boundary, so a text-extracted title can arrive masked; the validated bridge channel is not rewritten, and the template owns its screens on that channel (override refusal, contact-identifier refusal, bounds) |
| Numeric output policy | rounding/grouping grid on rendered numbers | byte-identical pass-through | byte-identical pass-through | this template renders no monetary aggregates (output = resource id, reference, project path, confirmation) — there is no precision grid to enforce, and rendered identifiers (issue/MR iids, numeric project ids, numeric label/branch tokens) must never be grouped, rounded, or rewritten; pinned by the numeric-fidelity tests |
