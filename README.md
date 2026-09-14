# Omnigent triple-stamp

> **Status: reference implementation, not a clone-and-run tool.**
>
> This runs inside Databricks and depends on internal infrastructure. Step one
> of the quick start below resolves `omnigent` from an internal package proxy,
> the Opus auditor reaches Glean, Jira, Slack, Confluence and SAFE through the
> internal `dbexec` CLI, and Stage 1 needs a Cursor account entitled to Grok 4.6
> Extra High. Outside that environment the install will not complete.
>
> The design is the point, and it is all readable here: `config.yaml` is the
> routing contract, `agents/*/config.yaml` are the three worker prompts, and the
> enforcement lives in `.omnigent/isaac-launcher/triple_stamp_isaac_launcher.py`
> and `.omnigent/runtime-python/triple_stamp_supervisor_runtime.py`.

Three models look at every request, they have deliberately different reach, and
only the third one gets to talk to you.

1. **Cursor Agent CLI** (Grok 4.6 Extra High) does the public-web research. It
   cannot see internal systems at all.
2. **Claude Opus 5** (max effort) tries to break that evidence, and checks it
   against internal systems. It cannot browse the public web.
3. **Codex** (GPT-5.6 Sol Ultra) decides whether the answer holds up, then
   writes it. It does no research of its own.

The nice thing about splitting reach that way is that neither researcher can
quietly cover for the other. If Cursor cites a doc that does not actually say
what it claims, Opus is the one who finds out.

Then a response policy compares what the supervisor tries to say against Codex's
stamped `shippable_answer`, byte for byte. If they do not match, it is denied. So
you either get an answer that survived all three stages, or you get an explicit
failure. You do not get a guess dressed up as an answer.

## Quick start

Three commands, and you only ever repeat the last one.

```bash
uv tool install omnigent==0.12.0   # once
cursor-agent login                 # once, Cursor needs a logged-in account
./triple-stamp                     # every time
```

That is the whole startup. `./triple-stamp` opens the browser UI, and the answer
shows up there once the pipeline stamps one.

If you want to check the install before spending anything, run
`./triple-stamp --self-test`. It runs the full regression suite and every bundle
validator inside the real sandbox, and it starts no models, so it costs nothing.

Prefer one question on stdout instead of a browser?

```bash
./triple-stamp -p 'Your question here'
```

Worth knowing about timing: a real run usually takes somewhere between fifteen
minutes and an hour, because Opus's internal audit is genuinely slow. The UI
narrates each stage as it starts, so you can tell it is working rather than hung.

### You probably do not need a voice profile

By default there is not one, and nothing asks you for one. Codex just writes the
answer in plain, direct prose. That is the path most people will use and it is
fully supported.

If you do want the final answer written in a particular voice, point
`TRIPLE_STAMP_VOICE_PROFILE` at a markdown file that describes it:

```bash
export TRIPLE_STAMP_VOICE_PROFILE="$HOME/my-voice.md"
```

There is a working example at `tests/fixtures/example-voice-profile.md` you can
copy and rewrite. Codex reads the live file on every run and reports its
SHA-256, so an edit takes effect on your next run and you can see in the ledger
which bytes it actually used.

One thing to know: if you set the variable and the file cannot be read, the run
fails instead of quietly shipping unstyled prose. That is on purpose. Asking for
a voice and silently not getting it seemed worse than stopping.

### The rest of the environment, all optional

```bash
# Deeper bound. Only 2 (default) and 4 are accepted, and cycle 5 is always
# denied. The strict $50 budget applies either way.
TRIPLE_STAMP_MAX_CYCLES=4 ./triple-stamp

# Launcher profile. `direct` is the default and uses plain `claude` and `codex`
# with whatever credentials they already have. `databricks` adds gateway,
# certificate, and token-refresh handling for Databricks-managed accounts, and
# needs to know which workspace to talk to.
export TRIPLE_STAMP_DATABRICKS_HOST=your-workspace.cloud.databricks.com
export TRIPLE_STAMP_DATABRICKS_PROFILE=your-cli-profile
TRIPLE_STAMP_PROVIDER=databricks ./triple-stamp
```

`run-with-isaac` still works and forwards to `./triple-stamp`. The old name is
kept so existing muscle memory and scripts do not break.

### Browser or interactive terminal

`./triple-stamp` starts Omnigent's supported local
server + host-daemon architecture inside the launcher's private Seatbelt and
per-run state directory. The `--no-session` request remains ephemeral at the
outer-launcher boundary, but is not forwarded to Omnigent's legacy direct-runner
path because that path cannot back the new-chat host picker.

The launcher creates a host-bound conversation and keeps two paths independent:
`HOME` remains inside the private per-run directory for credentials and config,
while the host daemon's cwd, `PWD`, browser filesystem root, and default working
directory remain the repository root. The browser picker is
confined to the generated agents' permitted project roots; it does not expose
the temporary credential home as a workspace.

Before printing or opening a URL, the launcher checks `/v1/hosts`, `/v1/agents`,
`/v1/sessions/<id>`, `/v1/sessions/<id>/agent`, and the selected host's
`/v1/hosts/<id>/filesystem` response without inference. It also sends a
non-draining `sys_read_inbox` capability probe through
`/v1/sessions/<id>/mcp`. It fails early unless the host is online, the selected
agent is readable, the session and host cwd satisfy the agent's required path,
the browser's candidate directory is the project root, the runner has created
that session's parent inbox, and all send prerequisites are true. On success it
normally opens:

```text
http://127.0.0.1:<port>/c/<conversation-id>
```

The root new-chat page on the same server can then start Triple-stamp or any
other agent whose harness is available on the registered local host. A message,
agent, online host, and working directory are all required before Omnigent
enables the send button. Each runner provisions a separate inbox when a browser
session first opens its stream, submits an event, or performs the no-inference
capability probe. Omnigent's normal session delete path removes the queue.

Use the terminal prompt or the browser. Enter `/quit` in the terminal to stop
the run; the launcher reaps the host, server, and runners and removes their
private registry and database. Restart with:

```bash
cd omnigent
./triple-stamp
```

Do not run a separate persistent `omnigent host` for this project.

### One-shot terminal

One-shot mode remains Omnigent's direct ephemeral path. It does not start or
advertise a browser host:

```bash
./triple-stamp -p 'Your task here'
```

It prints only the validated stamped answer (or the exact sanitized terminal
failure) and exits.

The launcher rejects model, harness, server, profile, resume, and system-prompt
overrides.

## Authentication

Every invocation proves authentication before taking the workspace lock or
starting the pipeline. Checks run inside the exact private HOME, PATH,
KUBECONFIG, wrappers, and outer Seatbelt used by the real run. Direct mode
requires executable `claude` and `codex` binaries and one trivial round trip
through each. Databricks mode retains the full certificate, token-refresh,
gateway, and provider checks below.

If a check fails, exit code `77` prints one remediation command and no secret:

```bash
# Cursor login or model entitlement
cursor-agent login

# Databricks certificates
dbcert --force --update-kubeconfig=false

# Claude SDK gateway and Isaac/Opus
isaac --claude

# Isaac Codex provider and token helper
isaac codex --no-omni

# Omnigent control plane (databricks profile only)
databricks auth login --host https://<your-workspace>.cloud.databricks.com \
  --profile <your-profile>
```

## Mechanical pins

| Role | Public Anthropic namespace | Databricks gateway namespace | Effort |
|---|---|---|---|
| Supervisor (`claude-sdk`) | `claude-sonnet-4-6` | `system.ai.claude-sonnet-4-6[1m]` | `low` |
| Workhorse (`cursor-native`) | `cursor-grok-4.6-xhigh` | `cursor-grok-4.6-xhigh` | Extra High |
| Auditor (`claude-native`) | `claude-opus-5` | `system.ai.claude-opus-5[1m]` | `max` |
| Judge (`codex-native`) | `gpt-5.6-sol` | `gpt-5.6-sol` | `ultra` |

The launcher profile and Claude model namespace are independent. Plain
`claude` launchers can use either column because root-owned managed settings or
environment can route them through a gateway. Resolution is explicit
`TRIPLE_STAMP_*_MODEL`, then the profile mapping, then detection of
`ANTHROPIC_BASE_URL`/`CLAUDE_CODE_USE_GATEWAY`; Codex remains
`gpt-5.6-sol` with no namespace detection.

The supervisor deliberately does not use Claude's native TUI. Omnigent's
supported Claude Agent SDK path uses external Claude authentication with the
plain launcher profile and the `isaac-databricks-ai-gateway` provider with the
Databricks launcher profile. Both use the SDK's asynchronous child drain. There is no supervisor PTY,
prompt-glyph readiness window, inbox poll, timer, or scheduled prompt.

Cursor remains native because Stage 1 requires Cursor Agent CLI login and its
first-party `WebSearch` and `WebFetch`. Its wrapper:

- accepts only `cursor-grok-4.6-xhigh`;
- seeds the observed base `grok-4.6` with `effort=xhigh` and `fast=false`;
- requires a private mode-`0600` refreshable credential store;
- bounds prompt acceptance to 45 seconds;
- blocks on process completion after startup, with no long-run polling.

The root `AGENTS.md` constrains only that Stage 1 `cursor_workhorse` runtime.
Human-directed repository analysis, architecture, build, and fix sessions use
the model requested by the user; Ryan's current repository-work model is
GPT-5.6 Sol Extra High. This does not change the runtime Grok pin above.

Opus remains native so the intended Glean, Slack, Confluence, JIRA, and SAFE
MCP tools remain available. Managed Claude settings defer MCP schemas
behind built-in `ToolSearch`, so that is the only built-in exposed by
`--tools`; Bash, shell, local runtime inspection, nested agents, file tools, and
public-web tools remain disabled. Native Claude also receives
`--setting-sources ""`, which excludes user/project/local settings and plugins
while retaining managed policy plus Omnigent's explicit `--settings` hooks.
Runtime pins are validated by the launcher rather than re-audited by Opus.
Native Claude starts with `--strict-mcp-config` and an immutable run-scoped JSON
file generated from only those five direct `.config/mcp`/`dbexec` server
definitions, so unrelated ambient or marketplace-plugin MCPs are not exposed.
Omnigent's temporary bridge `--mcp-config` is replaced rather than retained, so
every Opus process receives that strict catalog. The launcher performs no
protocol health check, auth probe, server prewarm, freshness/signature gate, or
background refresh. MCPs do not have to be available before Opus starts. If a
relevant source is absent, denied, or unavailable, Opus records that limitation
in its structured audit and returns `FAIL`, `NEEDS_WEB`, or `PASS_WITH_GAPS`.

The validator checks the exact hardened argv and immutable five-server config
without launching servers or sending a model request. First audits and fresh
web re-audits use the same launcher contract. Opus dispatch performs only local
strict-config materialization before returning the argv to Omnigent.
`ToolSearch` is explicitly pre-approved for `dontAsk`; managed policy owns the
20 internal read-tool allows.
All nine advertised writes are denied explicitly: Glean go-link creation,
Slack/JIRA writes, Confluence create/update/reply, and SAFE write/merge. Codex
receives no `--search` flag and is instructed to judge only the evidence
supplied by Cursor and Opus.

Every Opus audit reports `internal_sources_consulted` with the source system,
URL or record ID, supporting evidence, and retrieval timestamp. An empty list
requires `internal_sources_not_required_reason`. Roadmap, internal, and
customer-specific requests require at least one relevant Glean search; other
MCP families remain task-sensitive. Codex cannot stamp an explicitly
internal/roadmap request when Opus reports no internal source. The completed
Opus event stream also supplies an objective count of observed `mcp__` calls to
the Codex handoff; zero is an audit gap for internal tasks, not a launch failure.

The generated runner enforces the selected provider profile. Direct mode keeps
the plain `claude` command while injecting the same strict Opus MCP arguments;
Databricks mode keeps the existing Isaac launcher and fails closed if it is
missing or malformed.

After every completed Opus audit, web re-audit, internal lookup audit, or format
repair, the runner resolves that exact child bridge to its pinned Claude session
and reads only that session's raw JSONL transcript under the current isolated
HOME. It records `opus_effort_observation` with `status`, `outcome`, `expected`,
tri-state `compliant`, observed `values`, row count, child/session identity, and
a run-relative transcript reference. All observed assistant values equal to
`max` is good. Any observed non-max value is a material Codex REWORK signal and
blocks STAMP. Missing, unreadable, mismatched-child, or out-of-run evidence is
`not_observed` with `compliant: null`; it is advisory and is never inferred as
low effort. There is no transcript glob fallback or conversation-store lookup.

Opus receives Claude Code's supported `DISABLE_AUTOUPDATER`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, and `DISABLE_TELEMETRY` controls.
Isaac's model-serving OAuth helper and direct MCP authentication remain enabled.
The outer Seatbelt deliberately does not grant
`/tmp/cached_hcvault_token`: that host cache is unnecessary for this path, and
no token is copied, redirected, mounted, or exposed to Cursor, Codex, or the
supervisor. Isaac versions that still attempt optional Logfood/SecFood refreshes
may report a denied background refresh; those optional diagnostics are not part
of the Opus launch contract.

If a native child launch fails, that transition is terminal. The supervisor
does not brute-force another payload or title. `/quit` reaps the run-scoped
process tree before a new top-level run.

## Completion and output

`sys_session_send` returns an asynchronous handle. Omnigent parks the SDK
supervisor, waits for the worker, writes the complete child result to the
parent inbox, and wakes the supervisor with a notification. The supervisor then
calls `sys_read_inbox` to collect it. Project policy does not authorize,
classify, compare, or reject either routing call. Instead, the supervisor's
mechanical capability surface contains only `sys_session_send`,
`sys_read_inbox`, and Claude SDK ToolSearch for discovering those two schemas.
Shell, files, web, session inspection, schedulers, skills, browser tools, and
ambient MCPs are not advertised or callable by the supervisor.

One-shot mode removes both Omnigent 0.12's unrelated 30-minute loop timeout
and its local 30-extra-turn transport guard. Pipeline lifetime remains bounded
by the generated two-cycle default (or explicit four-cycle mode), the dynamic
verified root-supervisor routing policy, strict $50 budget, and per-stage
worker limits; the CLI
transport no longer exits successfully while a valid routed child is still in
flight.

A run-local ledger observes dispatches and collections without gating routing.
Collected packets get immutable private artifacts with byte counts and SHA-256
digests. Native Omnigent argument and launch errors pass through unchanged.
At the collection/evaluation boundary, the runtime carries the same populated
Opus child ID into both effort and MCP observers. The authenticated server API
fetches that exact child's items, deduplicates call IDs, and records actual
tool names and counts for Glean, Jira, Slack, Confluence, and SAFE. Claimed
`tools_called` names are mechanically checked against those events before
Codex; an unavailable observer remains an explicit advisory tri-state and
never fabricates a zero count or a nonexistent tool.
Raw counts include real failed `mcp__*` calls; each call separately records
whether a matching result event was observed.
For Cursor, the project-owned generated runtime replaces Omnigent 0.12's
stop-hook boundary: a native `stop`/idle marker remains pending until the exact
child's current transcript has an unchanged successful `turn_ended` event and a
non-empty final assistant result. Tool activity and progress invalidate an
early marker. Completion and delivery state is persisted by child session and
work id, so repeated hooks and runner restarts cannot redeliver the same turn.

`sys_read_inbox` never waits for Cursor. Normal reads contain only complete
packets and return promptly. A legacy empty packet is synchronously requeued as
a non-terminal lease before the read returns; the child delivery bit is reopened
so the existing native forwarder can issue the later real-completion wake.
Cancellation during policy evaluation also requeues the leased packet before it
propagates. The native forwarder, outside any MCP request, enforces five-minute
inactivity and fifteen-minute absolute stage bounds. Either timeout emits one
explicit terminal failure and one parent wake. Cursor's resident interactive
process is not itself a completion signal.

A normal no-rework chain needs six calls: one dispatch and one isolated inbox
read each for Cursor, Opus, and Codex. NEEDS_WEB uses deterministic
`cursor-web-{opus|codex}-N-H` titles. Codex keeps its existing named
continuation. Every Opus re-grade launches a fresh native child titled
`audit-cycle-N-web-H`; its self-contained handoff includes the original request,
original Cursor packet, previous audit/hunt spec, cumulative web evidence, and
new packet. It never depends on the completed `audit-cycle-N` terminal. Each
requester gets at most two web hops per cycle. Opus FAIL still goes
to Codex for final adjudication. Malformed Opus or Codex output gets one
normalization-only repair per cycle. Opus verdict extraction accepts plain,
fenced, or prose-wrapped JSON and structured YAML-style NEEDS_WEB output when
it includes a real hunt specification; cosmetic wrappers do not trigger repair.
These routes and bounds are supervisor instructions plus observable ledger
state, not natural-language authorization rules. No project policy counts stage
names, searches for packet substrings, requires byte reproduction in a handoff,
or blocks a send/read based on route state. Opus and Codex must semantically
reject incomplete evidence. The final STAMP attestation still requires a
completed Cursor -> Opus -> Codex chain in the stamped cycle.

The installed Omnigent 0.12.0
`max_tool_calls_per_session` built-in increments
`_policy_tool_call_count` for every tool call in the current conversation's
persisted `session_state`. It has no parent-only scope, tool-name exclusion, or
propagation switch. Function-policy config also has no first-class selector for
root versus child conversations, and its callable event omits conversation
identity. Triple-stamp child conversations remain bound to the root agent spec,
so copies of the built-in run for Opus ToolSearch/MCP work as well as supervisor
routing. The built-in itself does not sum a spawn tree: each conversation gets
its own persisted counter. Triple-stamp's run-local observation ledger is what
aggregates those parent and child events. The retained cycle-4-start fixture
records that mixed view: 174 calls, only 26 of them supervisor route calls, with
148 child-work calls.

The project therefore uses `supervisor_route_call_limit`. A narrow runtime
bridge copies Omnigent's current and verified root conversation IDs into the
structured function-policy event. The policy counts only when both IDs are
present and equal, and only for `sys_session_send`, `sys_read_inbox`,
`ToolSearch`, or the observed `sys_agent_start` framework alias. Child
Cursor/Opus/Codex ToolSearch and MCP calls do not increment it. Missing identity
or state abstains without fabricating a count; only a verified persisted
supervisor count can deny a call. Policy rebuilds and runner/server restarts
reload that count from root `session_state`. Routing remains free of content and
title gates.

The exact formula is `cycles * 19 * 2 + 8`. Each cycle has 19 scoped
transition slots: three for the
straight Cursor/Opus/Codex chain, four for two Opus NEEDS_WEB hops, four for two
Codex NEEDS_WEB hops, four for two Codex NEEDS_INTERNAL hops, and four reachable
one-shot format-repair/convergence allowance. Every child transition uses one
send and one isolated inbox read. Eight fixed slots cover schema discovery,
observed agent starts, and finalization/transport. The default cap is
`2 * 19 * 2 + 8 = 84`; explicit four-cycle mode is
`4 * 19 * 2 + 8 = 160`. Final Codex collection is already in the straight
chain. Cycle 3 is denied by default and cycle 5 is denied in deep mode.
Hop or cycle exhaustion terminates with honest validation gaps; an invalid
repair is an infrastructure failure. The $50 gate remains the primary bound
on child research.

Codex punch lists are persisted with canonical IDs built from gap type, claim,
required capability/source, and requested proof. Exact canonical-ID set
equality across consecutive cycles is the deterministic convergence threshold.
Equivalent internal gaps route to a fresh Opus audit, never Cursor. Equivalent
form-only gaps receive one bounded Codex convergence adjudication, which must
STAMP a supportable answer with named limitations or terminate without another
full cycle.

Codex must return one JSON object under 10,000 characters. On `STAMP` it must
include citations, a non-empty `shippable_answer`, and a live profile check for:

the file named by `TRIPLE_STAMP_VOICE_PROFILE`, when one is configured.
With voice rendering off, that check reports empty values instead.

A configured profile is reread on every judgment, SHA-256 checked, and
mounted read-only. Collection of a valid Codex `STAMP` creates an exclusive run-scoped
attestation tying the answer bytes to the Codex packet, child, cycle, and live
profile digest. In one-shot mode, child stdout is captured and the outer
launcher emits the attested answer exactly once, so model-written success prose
is never trusted. Infrastructure failures and exhausted rework cycles create
an immutable failure attestation and exact sanitized terminal result; one-shot
mode relays that result instead of a generic missing-STAMP message. Outside
`--self-test`, the launcher returns zero
only when the attestation, collection ledger, answer bytes, and current profile
all agree.

## Isolation and cleanup

- Omnigent `0.12.0` and Claude Agent SDK `0.2.152` are checked. Databricks
  mode additionally requires Isaac `2.x`.
- Each run gets a private HOME, state DB, temp tree, PEX root, kubeconfig, and
  short harness socket path.
- Cursor gets a second provider-specific HOME. It cannot discover the Claude
  and Codex plugin/skill trees that authentication preflight materializes.
- Provider selection, Claude/Isaac controls, Codex paths, and remote runner
  tokens stay in Omnigent's runner environment and are scrubbed from every
  Cursor Agent child environment.
- A macOS Seatbelt encloses the full process tree while preserving PTYs,
  loopback, required runtime paths, and read-only access to the voice profile.
- Writes to the real home and real kubeconfig are denied by pre-model probes.
- A project lock rejects concurrent runs because Cursor uses workspace-level
  bridge files.
- Signals are forwarded to the process group. Run-marked descendants are
  terminated and reaped, Cursor bridge files are restored, and successful
  runtime directories are removed.
- Unexpected post-model failures retain private diagnostics until the next
  healthy locked launch.
- The native Opus wrapper is a project-owned `omnigent.claude_launcher` entry
  point. A shared lock makes its editable install idempotent; healthy runs do
  not reinstall it, stale project metadata is repaired, and unrelated packages
  are never removed.

Cursor's native approval mirror can misread a long-lived Run Everything marker
as a manual approval. The same project-owned runtime suppresses only that
false-card fallback, closes the parent-inbox startup race, restricts the
supervisor's advertised tools, records routing metadata, and provides the
transcript-backed completion boundary. It does not select, reorder, authorize,
or reject routing payloads and does not modify the installed Omnigent package.

The cumulative cost policy reads each unique session's own persisted usage row
once. Provider-reported USD is counted as reported; only model buckets without
a USD field receive a cache-aware conservative estimate. Propagated
`policy_cost_usd`, subtree snapshots, and duplicated event-local totals are not
summed into spend. Before every worker dispatch, a model/stage/handoff-size
reserve must fit in the remaining $50 or dispatch is denied before more spend.
Budget state and terminal errors retain reported, estimated, remaining, stage,
cycle, reserve, and denial reason.

The validator runs its boundary probes in a nested temporary state directory,
and the launcher removes all pipeline outputs again immediately before the
first model. A validator fixture can therefore never become the immutable
terminal result of the following real run.

## Verification

`--self-test` is non-inference. It checks parser/validator output, models,
efforts, harnesses, provider resolution, wrappers, auth paths, tool
restrictions, async completion ordering, exact relay, failure terminality,
startup bounds, long Cursor work, call cap, Seatbelt probes, concurrency,
cleanup contracts, optional Opus MCP behavior, and fresh Opus web re-audits.

`./verify-provider-matrix` runs the complete regression suite outside Seatbelt
with the provider unset (default direct) and with explicit Databricks, then
runs both profiles' full bundle validator and regression suite inside the exact
generated Seatbelt via `--self-test`. It never starts the paid pipeline.

A release is not complete until this real public-doc smoke passes:

```bash
./triple-stamp -p \
  'Using current official Cursor documentation, identify the documented Cursor Agent CLI flag for unattended command execution. Stage 1 must use WebSearch and WebFetch and return the official URL, exact quote, and retrieval date. Audit and stamp only if the fetched source supports the answer.'
```

Inspect runtime metadata for stage order, exact models and efforts, Opus MCP
access, Codex `STAMP`, byte-exact relay, routing-call count, cost, absence of
approvals/polls/schedulers, and cleanup.

## Limits

- macOS only.
- Direct mode has been exercised only on Ryan's machine, which still has
  Databricks-managed Claude settings and Codex configuration. A clean personal
  Anthropic/OpenAI account remains an unproven open-source acceptance test.
- `gpt-5.6-sol` with Ultra effort has not been proven available to a
  non-Databricks Codex account. Direct preflight tests the configured account
  and fails closed instead of substituting another model.
- Direct mode does not refresh Databricks model-serving tokens. A long run on
  a managed gateway could outlive its token; use the `databricks` profile
  when token/certificate lifecycle support is required.
- Opus's Glean/Jira/Slack/Confluence/SAFE MCP definitions still use internal
  `dbexec`; making internal research optional for non-Databricks users remains
  separate work.
- The outer Seatbelt is one union boundary for the whole process tree, not a
  separate filesystem compartment per stage.
- Network is available to the tree for model APIs, Cursor public web, and Opus
  internal MCPs. Stage restrictions are capability surfaces and the outer
  sandbox, not an
  egress firewall.
- The `$50` policy is a hard admission/between-call stop. Reported cost is used
  when present; only genuinely unpriced tokens use conservative per-family
  ceilings. A pre-dispatch reserve materially reduces one-call overshoot risk,
  but a provider that supplies usage only after completion cannot be
  transactionally stopped at an exact dollar mid-generation.
- Opus still uses the native Claude TUI and therefore retains Omnigent's
  terminal startup behavior. The routing-only supervisor, which starts every
  run and previously failed at that boundary, does not.
