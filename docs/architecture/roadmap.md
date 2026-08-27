# Agentic Copilot and Controlled Execution Roadmap

- Status: Milestones 1-3 implemented; Milestones 4-5 planned
- Last updated: 2026-08-27

## Purpose

This roadmap sequences the accepted architecture into independently reversible
milestones. Milestones 1 through 3 are implemented. The simulator remains the
default, and the bounded local capsule can execute approved typed operations
only against its private mock endpoint. Later milestones remain plans rather
than implementation-status claims.

Each milestone must preserve the authority split in [ADR-0001](decisions/0001-copilot-orchestration-boundaries.md)
and the fail-closed execution boundary in [ADR-0002](decisions/0002-execution-credential-boundaries.md).
Do not start live Google Cloud execution or choose a managed runtime under this
roadmap; both require a later decision record.

## Delivery Rules

- Implement the smallest complete slice that passes its acceptance gate.
- Keep each behavior change, contract change, refactor, and dependency change in
  an independently reversible commit.
- Stage explicit paths, inspect the staged diff, and run the focused checks before
  each commit. Never stage the repository wholesale.
- Use commit subjects in the form `verb(service): atomic one sentence commit`.
- Do not modify or stage `agentctf/`, either external submodule, generated output,
  credentials, local scenarios, caches, or unrelated files.
- One coordinating agent owns shared contracts, dependency metadata, CLI
  integration, public documentation, and final verification. Parallel agents are
  limited to independent components with non-overlapping files and tests.

## Milestone 1: Execution Foundation

Implementation completed on 2026-08-26. The execution boundary now uses opaque
credential references, typed credential sources and leases, immutable action and
execution contracts, atomic approval-attempt records, and a common provider
protocol with the deterministic simulator as its only built-in provider.

### 1A. Replace embedded credentials

Change the strict scenario contract from `access_token` to an opaque
`credential_ref`. Do not provide a deprecated field, automatic conversion, or
compatibility flag. Add a typed parser for only `stdin`, `file:<path>`,
`env:<name>`, `adc`, and `impersonate:<principal>` source descriptors.

Keep source loading in an application service under the execution boundary. The
runner supplies descriptors and references but never reads, logs, or persists
credential values as CLI business logic.

Suggested checkpoints:

- `replace(runner): require credential references in scenario files`
- `add(execution): resolve credentials through typed sources`

Acceptance gate:

- Scenario validation rejects embedded `access_token`, a missing or empty
  `credential_ref`, unknown fields, and invalid source forms.
- Raw tokens are never accepted as command arguments.
- `stdin`, `file`, and `env` tests use synthetic short-lived token values;
  `adc` and impersonation tests use fakes without network access.
- Missing sources, unreadable files, empty values, expired leases,
  unverifiable principals, and principal mismatches fail before provider calls.
- Tests prove token values do not appear in persisted JSON, traces, errors,
  process arguments, captured model inputs, or output summaries.
- Temporary values and files are cleaned on success, failure, cancellation, and
  interruption.

### 1B. Add typed execution contracts

Add shared immutable `ActionDefinition` and `ExecutionSpec` models and extend the
existing `ExecutionObservation` only with provider-neutral fields. Add a
runtime-only typed credential lease. Register each action with a concrete
parameter model; do not add a generic command or free-form parameter escape
hatch.

Update producers, consumers, fixtures, serialization tests, and documentation in
the same contract revision.

Acceptance gate:

- Model validation rejects unregistered actions, arbitrary commands, unknown
  parameters, wrong parameter types, and nondeterministic serialization.
- Existing approval digests bind the complete typed parameter payload.
- Persisted records retain stable ordering and contain opaque references only.
- Contract tests cover round trips and backward compatibility for unchanged
  persisted records.

### 1C. Introduce provider dispatch

Define `ExecutionProvider`, refactor the simulator behind it, and make provider
selection part of application assembly. Preserve a single common path for the
kill switch, authorization, approval matching, action resolution, atomic attempt
reservation, credential resolution, provider invocation, observation
validation, and record finalization.

Suggested checkpoint:

- `refactor(execution): dispatch requests through a provider protocol`

Acceptance gate:

- Existing simulator outcomes remain deterministic.
- Unknown or unavailable providers fail during assembly without falling back.
- Approval mismatch, stale state, stale matrix, disallowed target, unknown
  action, invalid parameters, duplicate consumption, and the kill switch all
  prevent provider invocation.
- Starting a provider attempt consumes the exact approval. Failure and timeout do
  not permit replay without a new validation and approval.
- Provider failures are translated to existing neutral errors with redacted
  context.

## Milestone 2: One Typer CLI

Implementation completed on 2026-08-26. One installed `verisentinel` Typer
application now owns scenario validation, credential inspection, corpus build
and status, sandbox diagnostics, and the direct human-gated run workflow. Thin
command handlers call application services and translate errors to the defined
process exit codes.

Replace the `argparse` entrypoint with one Typer application. CLI handlers parse
input, call application services, render bounded output, and translate known
errors to exit codes. They do not perform validation, credential resolution,
corpus construction, orchestration, or execution policy themselves.

Initial command surface:

```text
verisentinel scenario validate PATH
verisentinel auth inspect --credential-source SOURCE
verisentinel corpus build
verisentinel corpus status
verisentinel sandbox doctor
verisentinel run --scenario PATH --credential-source SOURCE
```

`auth inspect` reports only non-sensitive source kind, verified principal, and
expiry metadata. `sandbox doctor` may initially report that no capsule provider
is installed. Preserve interruption behavior: an interrupt returns 130 and never
implies approval or execution.

Suggested checkpoint:

- `migrate(cli): expose planner workflows through Typer commands`

Acceptance gate:

- Top-level and nested help output is stable and contains no credentials or
  local configuration values.
- Tests cover every route, required option, invalid input, known runtime failure,
  successful exit, and interruption.
- Exit codes are consistent: 0 for success, 2 for invalid user input or
  configuration, 1 for an application failure, and 130 for interruption.
- The existing direct planner workflow remains reachable through `run` with the
  same explicit review semantics and simulator default.
- Adding Typer uses a bounded dependency version and does not introduce another
  CLI framework.

The local terminal shell now provides UI-only session inspection and resumption:

```text
verisentinel session list
verisentinel session show SESSION_ID
verisentinel session resume SESSION_ID
```

These records contain sanitized local command names and exit codes only. They
are separate from the future model-driven copilot session contract and do not
grant proposal, approval, or execution authority.

## Milestone 3: Local Execution Capsule

Implementation completed on 2026-08-27. The optional capsule provider now uses
the repository digest lock, a fixed typed-operation entrypoint, an internal
Docker network, bounded resources and output, temporary read-only inputs, and
guaranteed cleanup. Read-only readiness diagnostics and Docker integration tests
cover the complete approved execution path against the private mock endpoint.
See the [Milestone 3 usage guide](../usage/milestone03.md) for manual checks and
the reviewed measurement summary.

Implement a container `ExecutionProvider` exactly as constrained by ADR-0002.
The first capsule communicates only with a private mocked endpoint and executes
only repository-registered typed actions. Do not add a live cloud provider in
this milestone.

Suggested checkpoint:

- `add(sandbox): run approved specifications in ephemeral containers`

Acceptance gate:

- The image is pinned by digest, starts through a fixed entrypoint, runs as
  non-root, has a read-only root filesystem, drops capabilities, prevents
  privilege escalation, and receives only one read-only serialized spec.
- Tests or provider inspection prove configured CPU, memory, process, timeout,
  output, temporary-storage, and network bounds.
- The capsule cannot select a command, mount the repository, contact a public
  endpoint, or read model and repository secrets.
- The credential is available only through the fixed temporary file, is absent
  from arguments and environment values, and is removed with the container on
  every exit path.
- Timeout, malformed output, oversized stdout or stderr, endpoint failure,
  non-zero exit, and cleanup failure translate into bounded, redacted errors.
- A full integration test sends one approved spec to the mocked endpoint,
  returns a typed observation, records the consumed approval, and leaves no
  capsule or credential file behind.
- `sandbox doctor` reports runtime availability and each required isolation
  capability without making an external request.

Measure startup latency, total duration, peak memory, cleanup duration, and
failure behavior. Store only a small reviewed benchmark summary, not generated
runtime output, when those measurements inform the managed-runtime decision.

## Milestone 4: Optional DeepSeek Copilot Prototype

Define `CopilotHarness` with a deterministic fake before adding an external SDK.
Then implement an optional DeepSeek adapter and connect IAMouflage through its
existing read-only MCP server. Do not edit the IAMouflage submodule or expose
execution credentials and authority as model tools.

Select one exact DeepSeek SDK and matching runtime version or commit during
implementation and record it in dependency metadata and the adapter test. Keep
the dependency in an optional extra so the deterministic workflow installs and
runs without it.

Suggested checkpoint:

- `prototype(copilot): adapt pinned DeepSeek sessions through MCP`

Acceptance gate:

- The repository protocol supports starting, inspecting, resuming, stopping, and
  tracing a session without provider-specific types crossing its boundary.
- A readiness smoke test starts the configured MCP service, waits for tool
  registration, and verifies the exact expected read-only tool names before the
  first model turn.
- Missing tools, startup races, version mismatch, timeout, SDK absence, and
  subprocess failure stop only the copilot session and leave deterministic state
  unchanged.
- Tests prove model inputs and session logs contain no credential values,
  credential-source locators that reveal protected paths, approval authority, or
  provider invocation tools.
- The session records the adapter version, IAMouflage corpus revision, and
  immutable matrix version.
- The current direct workflow passes its full regression suite with the optional
  dependency absent.
- The integration remains labeled experimental while the upstream project is in
  developer preview.

## Milestone 5: Managed Execution Decision

After the local capsule gate passes and measurements are reviewed, write a new
ADR that selects or rejects a managed runtime. Compare isolation, attached
identity, federation, impersonation, network policy, timeout and output bounds,
cleanup evidence, auditability, cost, and operational complexity.

Acceptance gate for the decision, not an implementation:

- The comparison uses measured local-capsule data and current primary provider
  documentation.
- The identity design prefers attached identity, Workload Identity Federation,
  or impersonation and explains any supplied-token compatibility requirement.
- The threat model accounts for a service-account access token remaining valid
  until expiry after local cleanup.
- Network destinations, resource scope, audit records, retry semantics, and
  emergency disablement are explicit.
- No live provider code is merged before the ADR is accepted and a separate
  implementation gate is defined.

## Verification Matrix

Run focused tests after each behavior slice, then the broadest relevant checks
from `code/`:

```bash
.venv/bin/ruff check core ingestion environment validator proposer green_agent launchpad execution runner tests
.venv/bin/python -m pytest
```

Run evaluation suites when their contracts, inputs, or workflows are affected:

```bash
.venv/bin/python -m pytest evaluation-pipeline/tests -q
.venv/bin/python -m pytest evaluation-tests/tests -q
```

Before closing any milestone:

- review the unstaged and staged diffs for unrelated or generated files;
- search changed files for credentials and non-neutral terminology;
- verify root and representative nested `AGENTS.md` composition remains below
  the default 32 KiB limit;
- confirm protected submodule revisions did not change;
- review `git log --oneline` for atomic ordering and commit-subject conformity;
  and
- report any command that could not run with its exact failure and the closest
  completed verification.
