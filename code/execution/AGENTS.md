# Controlled Execution Guidance

## Scope and Security Boundary

`execution` is the fail-closed boundary between an approved request and a
provider. It owns action resolution, authorization checks, credential leasing,
provider invocation, observation validation, and execution records.

## Execution Contracts

- Accept only registered `ActionDefinition` values and immutable, typed
  `ExecutionSpec` values. Do not accept arbitrary commands, shell fragments,
  unregistered action identifiers, or untyped parameter dictionaries.
- Preserve exact approval binding, state and matrix version checks, explicit
  target allow-lists, the kill switch, and one-time approval consumption.
- Define providers behind a narrow `ExecutionProvider` protocol. Provider
  selection must not weaken the common authorization path.
- Keep the simulator deterministic and available as the default provider until
  another provider satisfies its acceptance gate.
- Translate provider output into a bounded, typed `ExecutionObservation` before
  it reaches orchestration or persistence.

## Credential Isolation

- Persist and pass only opaque credential references outside this package.
- Resolve a reference into a short-lived credential lease only after all static
  authorization checks pass and immediately before provider invocation.
- Verify lease expiry and expected principal. A missing, expired, or mismatched
  lease must stop execution.
- Never accept raw credentials as command arguments or place them in scenario
  files, persisted JSON, traces, errors, process arguments, or model context.
- Redact provider errors and output before tracing, returning, or persisting
  them. Treat derived authorization headers and temporary credential files as
  credentials too.
- Limit credential material to the selected provider call and guarantee cleanup
  on success, failure, timeout, and interruption.

## Provider and Capsule Rules

- A local capsule must use a fixed entrypoint and one serialized execution spec;
  it must not expose a general-purpose interactive shell.
- Run capsules as non-root with a read-only filesystem, dropped capabilities,
  bounded CPU, memory, processes, duration, and output, and a restricted network
  destination.
- Mount credential material through a temporary provider-owned file or equivalent
  isolated channel and remove it during guaranteed cleanup.
- Keep model API keys, repository secrets, and copilot session state outside the
  execution environment.

## Verification

Test embedded-secret rejection at the input boundary, invalid references,
expired leases, principal mismatches, unknown actions, parameter validation,
approval mismatches, stale versions, duplicate consumption, kill-switch
behavior, redaction, cleanup, timeout, and bounded output. Assert that provider
fakes are not called when any earlier check fails.
