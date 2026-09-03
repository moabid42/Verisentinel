# Application Guidance

## Scope

These instructions apply to the maintained Python application under `code/`.
They supplement the repository-root guidance. The `IAMouflage/` directory is an
external submodule and is outside this guidance unless a user explicitly places
it in scope.

## Component Ownership

- `core/` owns cross-package Pydantic contracts, identifiers, persistence
  primitives, configuration, shared errors, security helpers, and tracing.
- `ingestion/` owns source normalization and immutable matrix construction.
- `environment/` owns versioned authorization state and state transitions.
- `validator/` owns deterministic feasibility and coverage decisions.
- `proposer/` owns model-assisted ranking of cataloged techniques. It does not
  authorize candidates or execution.
- `green_agent/` owns engagement orchestration and deterministic workflow order.
- `launchpad/` owns operator presentation and explicit decision capture.
- `execution/` owns approval enforcement, action resolution, credentials, and
  provider invocation.
- `runner/` owns scenario loading and process assembly. Keep business rules in
  the application services that own them.
- `evaluation-pipeline/` and `evaluation-tests/` own their isolated evaluation
  harnesses. Do not couple production behavior to generated evaluation results.

## Shared Contracts and Dependencies

- Put immutable contracts used across package boundaries in `core/models.py`.
  Keep package-specific persistence or transport models in that package's
  `models.py`.
- Reuse the error types in `core/errors.py` and translate errors at package or
  transport boundaries without discarding their cause.
- Keep dependencies directed through typed contracts or small protocols. Avoid
  importing CLI, API, or persistence implementations into core decision logic.
- Preserve strict Pydantic validation, frozen records, stable identifiers, and
  deterministic serialization order.
- Update all producers, consumers, fixtures, documentation, and persisted-format
  tests when a shared contract deliberately changes.

## Change and Test Routing

- Changes to `core/models.py`, persistence formats, identifiers, or error
  behavior require main-suite regression coverage.
- Changes to orchestration require `tests/unit/test_green_agent.py` plus the
  directly affected service tests.
- Changes to execution require `tests/unit/test_execution.py` and security tests
  when credentials, redaction, or traces are affected.
- Changes to scenario parsing or command routing require
  `tests/unit/test_runner.py`.
- Changes under `evaluation-pipeline/` require its local test suite. Changes
  under `evaluation-tests/` require its local test suite.
- Run focused tests before the repository-wide Ruff and pytest commands from the
  root guidance.

## Coordinated Work

- Use parallel agents only for independent components with non-overlapping file
  ownership and acceptance tests.
- One coordinating agent owns changes to shared contracts, dependency metadata,
  top-level entrypoints, public documentation, and final integration.
- Do not have multiple agents edit the same model, fixture, migration, or
  persisted format. Sequence those changes through the coordinating agent.
- Integrate and verify each component against the same shared-contract revision
  before combining results.

## Generated and Local Files

Do not edit or stage `artifacts/`, `runtime/`, caches, local `.env` files,
scenario secrets, evaluation result directories, or submodule contents as part
of application changes.
