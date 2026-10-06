# Test Specification - CMN-C2-239 GitLab Issue & MR Agent

## Test Strategy
- Test types: Unit (per node + service + config + inner/outer graph + graph
  boundary) / Proof-of-Boundary (full outer-graph invoke, real ASGI HTTP entry,
  import isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end backbone coverage lives in PB-6 and the HTTP suite,
  which drive the real compiled outer graph).
- The GitLab call is exercised through the deterministic, network-free v1 stub
  transport (default) and through monkeypatched fake clients; no live GitLab call.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` - `BaseNode.__call__` routes the full security pipeline (trust
  gate -> input mask -> `execute()` -> output credential scan) - never bare
  `node.execute(state)`, EXCEPT where a test's subject is a guarantee the
  template itself must enforce with no framework wrapper in front (the
  instruction-override and caller-field refusals in PreProcessNode are proven
  via direct `execute()` for exactly that reason), plus the single documented
  exception: `CallGitLabApiNode.execute(state, config=...)` config-override test
  (a 2nd argument `__call__` cannot forward). State builders set
  `caller_trust_level = TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode
  (the single external gate) and `TrustLevel.ANONYMOUS.value` for every other
  node. The trust-rejection test asserts on the RETURNED error dict
  (`status == AgentStatus.ERROR.value`, "trust gate denied" in `error_log`,
  execute-only keys absent) - `__call__` never raises for a trust denial.
- Assertion contract (wheel `agenticstar-agentcore==1.0.1`): the invoke surface is
  `result["output"]` / `status` / `trace_id` / `correlation_id` / `node_history`
  (never `formatted_output` at the invoke surface); status is compared to
  `AgentStatus.SUCCESS`/`.value` (lowercase `success`/`error`); the outer graph is
  called as `invoke(user_input=..., ctx=..., input_context=...)`; free-text
  identifiers may be masked (`[MASKED]`) so text-path resource evidence is
  asserted by presence, not raw repr; audit spies assert on `call.args[1]` (the
  event payload), never the whole-call repr.
- Real-SDK pipeline behaviors encoded by the suite: `__call__` short-circuits on an
  incoming errored state (execute() is skipped; error status/error_log pass through);
  the framework mask rewrites Title-Case bigrams (across newlines), emails, and
  digit groups in `user_input`/`validated_input` to `[MASKED]` before `execute()`
  sees the text - positive free-text payloads are proper-noun-free, intentional-PII
  tests assert the `[MASKED]` path, and proper-noun fixtures travel through
  `input_context` (the validated structured channel, which is not rewritten - the
  boundary suite pins both directions).
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS error dict (`status == AgentStatus.ERROR.value`, "trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node (incl. the write-capable CallGitLabApiNode) declares ANONYMOUS |
| U-02 | test_pre_process_node.py | caller-data contract owner: serialize NL request into `validated_input` (JSON) + validated fields into `gitlab_request`; HTML strip; project alias priority project_id > project > project_path; instruction-override screen (token class `<\|...\|>` / `[INST]` / `<<SYS>>` + directive phrases, raw AND post-strip, parsed context incl. keys at any depth, `\u`-escape-proof, hostile names masked); per-field type/shape/length/entry-cap validation; finite+bounded `target_iid` (NaN / Infinity / bool / out-of-range matrix); contact-identifier refusal naming category not value; ordinary developer prose unaffected | refusals fail CLOSED via direct `execute()` (nothing carried forward, field named, value never echoed); proper-noun titles accepted on the structured channel; audit payload = presence + field names only |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`, `glpat-*`) | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string per the state contract); empty/short -> error; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | explicit validated contract intent wins; invalid contract value falls back to keywords; keyword intent (most-specific-first priority; EN + JA); **no-signal FAILS CLOSED** (every intent is a write - no read-only fallback, never a guessed write) | contract intent honoured; correct intent per keyword; comment beats MR mention; MR beats issue ref; no signal -> `status=error` + "could not classify", no intent key; empty -> error; audit emits intent label only |
| U-05 | test_infer_gitlab_fields_node.py | validated contract fields beat text extraction (title / description / labels / branches / comment target / project); project resolution (contract > text mention > shape-valid hint > `Project:` field; malformed left `""` - never invented); quoted title; description (prose + `Key: value`); labels; MR branch pair / KV branches (unresolved left empty); comment target `!N` > `#N` + `saying "..."` / `Comment:` body; GitLab REST v4 payload per intent (JSON string per the state contract) | issue `{title, description, labels}`; MR `{source_branch, target_branch, title}`; comment `{target_kind, target_iid, body}`; contract values arrive verbatim; unresolved project/branches/target left empty; missing input / missing / unknown intent -> error (fail-closed) |
| U-06 | test_call_gitlab_api_node.py | create/MR/comment via the network-free v1 stub; `gitlab_config` state field + `execute(state, config=...)` override (documented direct-execute exception); API error / unresolved project / missing title / missing branches / unresolved target / unknown intent / missing payload; secret posture (live transport refuses to run unauthenticated; token read via `ctx.secrets`, never env/state) | resource_id/resource_ref on success (`gitlab://<project>/issues/<iid>` style); 403 surfaces in error_log as the HTTP status only (the API's message is never echoed; a transport failure is logged by exception class, never by its text); live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to client; audit emits presence signals with `stub_transport=True` |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; ref/id formatting; title fallback | "Created GitLab issue / Opened merge request / Posted comment on ... ref=... id=..."; missing evidence -> error |
| U-08 | test_post_process_node.py | `formatted_output` shaping (scalar evidence fields only - the raw request payload is never surfaced); errored state passes through `__call__` un-masked (short-circuit); resource-evidence gate + credential scan (module-level `_security_gate_output()` helper, full-node path + direct function tests incl. Bearer- and glpat-shaped values, nested containers and the top-level control) | success shape without `gitlab_payload`; error status/error_log preserved, no success shape fabricated; SUCCESS without resource_id/resource_ref blocked ("output gate"); nested credential caught, clean nested structure passes; every non-success envelope is `{status, reason}` drawn from the module's declared constants - a sentinel seeded into `error_log` appears nowhere in the returned mapping |
| U-09 | test_gitlab_client.py | GitLab REST API v4 client: create issue / create MR / post note; URL-encoded `/projects/:id/`; `PRIVATE-TOKEN` header; `GitLabApiError` on non-2xx; unknown note kind -> ValueError; stub shapes (`_stub` marker, iid/title echo, note id/body echo, deterministic); `uses_stub_transport` | correct URLs/headers/bodies; 400 raises with extracted message; stub deterministic shapes |
| U-10 | test_config.py | flat `config/agent.yaml` manifest sanity + `config/config.yaml` runtime split + declared-value arrival | id CMN-C2-239, namespace cmn, Cat 2, CMN, ToolCallingAgent, single dotted `class` path, VERIFIED_EXTERNAL, `requires.secrets == []` (token via `ctx.secrets.get()`, optional at compile time); max_retry/timeout_s reach the constructor; the gitlab section reaches the inner graph; an on-disk rewrite of `gitlab.base_url` CHANGES the rendered output (arrival proven, not assumed) |
| U-11 | test_domain_workflow_graph.py | inner `GitLabWorkflowGraph`: identity, `_extra_initial_state()` gitlab_config JSON injection, `route()` error short-circuit, `get_output` contract, compile, direct inner invoke on the stub | name/state_schema correct; config forwarded as JSON string; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with resource evidence |
| U-12 | test_graph.py | outer `GitLabIssueMRAgent`: identity, `GitLabWorkflowGraphNode._parent_config()` runtime forwarding (never `{}`), `propagate_hitl=False`, non-raising error strategy, structured-product `get_output()` override (extend-not-replace; SUCCESS-only; output-gate re-scan fails closed) | gitlab section reaches `config["configurable"]` (backbone keys travel via the constructor instead); structured keys on SUCCESS only; credential-shaped value -> structured keys suppressed, `status=error`, `reason=output_withheld`; neither `error_log` nor the violation text is projected (a sentinel seeded into `error_log` appears nowhere in the envelope) |
| U-13 | test_graph_boundary.py | outer/inner boundary: error containment (inner rejection -> inner node's own message, unexpected exception -> generic message, compiled-path error envelope, no traceback / runtime path in the final state's error_log); context bridge end-to-end (Title-Case title via `input_context` arrives at the API call VERBATIM; the free-text control arrives `[MASKED]` - both directions pinned); explicit contract intent wins through the full graph; comment target crosses the bridge; numeric fidelity (rendered identifiers byte-identical - no grouping/rounding anywhere) | all contained; bridge preserves, text path masks; `#12345` stays `#12345`, never `12,345`/`12,000` |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: 0 platform-internal SDK imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external-trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, GitLabWorkflowGraphNode, PostProcessNode, FinalizeNode]`, resource evidence + confirmation in `result["output"]`, and the structured action keys (`intent=create_issue`, resource evidence) on the envelope; ANONYMOUS caller denied at pre_process (error, no post_process, no output, no structured keys); blank input -> error, not crash |
| PB (HTTP) | Real ASGI `/invoke` entry | test_pb_invoke_http.py | Bearer auth boundary (missing/wrong token -> generic 401; `/health` open); caller data via `input_context` produces a real resource (issue / MR / comment evidence + non-zero output); comment target iid byte-identical in the confirmation; validation rejection (non-finite `target_iid`) -> structured error envelope, value never echoed, no resource keys; instruction-override token form refused end-to-end; hostile context value refused; transport caps (too many keys / oversized) -> 413 |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (no graph class declares `propagate_hitl=True`; no cross-boundary interrupt() checkpoint): canonical skip stub - skipif with a clear reason, real AssertionError body (never `assert True`) so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct with runtime config + compile + provision_secrets at import); agent constructs + compiles via the supported path; `/invoke` + `/health` routes exposed |
| PB (error containment) | Error-envelope containment on both post_process error paths | test_pb_error_envelope_containment.py | incoming-ERROR path and both output-gate-violation arms (missing evidence / credential-shaped value): `formatted_output` present AND truthy (a falsy value re-activates the framework's `formatted_output or result` fallback) carrying the error status + a constant reason code only (a closed set: a sentinel seeded into `error_log` appears nowhere in the node delta or the shipped envelope, on every error path and on the `get_output()` re-scan refusal); no resource_id / resource_ref / resource_title / confirmation / merged `result` anywhere in the envelope; those fields cleared from STATE, not merely omitted; `get_output()` `output` slot empty on any non-SUCCESS; violation messages name field paths, never the matched value. Clean-path controls assert the success path still returns the answer and that the same leak probe DOES fire on it (no vacuous pass) |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy
> tests (every node's `<node>_complete` event asserted on the event payload,
> `call.args[1]`). PB-3 (live external service) is not in this suite - the v1
> transport is the documented network-free stub.

## Test Execution Summary
- Execution date: 2026-09-04
- Runner: pytest under the published wheel `agenticstar-agentcore==1.0.2`
- Total tests: 245
- Pass: 244 / Fail: 0 / Skip: 1 (PB-7 - auto-waived, non-HITL)
