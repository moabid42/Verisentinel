# Deterministic Orchestration Guidance

## Scope and Authority

`green_agent` owns the deterministic engagement state machine. Model-driven
components may propose or summarize, but they must not publish candidates,
approve requests, advance state, or invoke execution directly.

## Required Invariants

- Preserve explicit workflow transitions and reject operations that do not match
  the current engagement status.
- Bind every decision and execution request to the exact engagement, candidate,
  identity, target, action, validator result, state version, matrix version, and
  typed arguments.
- Validate candidates before publication and validate an approved candidate
  again immediately before registering execution approval.
- Publish no more than three admissible candidates for operator review.
- Require an explicit operator decision. Empty, malformed, stale, or mismatched
  input must never imply approval.
- Preserve the execution kill switch, one-time approval consumption, and
  engagement authorization disablement on termination.
- Apply an execution observation only to the expected environment state and
  produce a new immutable state version.

## Boundaries

- Depend on proposer, validator, launchpad, environment, and execution behavior
  through their existing services or typed gateways.
- Keep model sessions, provider SDKs, credential resolution, and transport
  details outside the orchestrator.
- Keep traces factual and redacted. Trace identifiers, versions, state changes,
  and decisions, but never credentials or model secrets.
- Prefer a small explicit state transition over callbacks, implicit retries, or
  generic workflow frameworks.

## Verification

Cover successful transitions and fail-closed paths, including stale versions,
approval mismatches, unknown candidates, duplicate decisions, termination, and
execution failures. Use deterministic fakes and assert that a rejected path does
not call the next authority boundary.
