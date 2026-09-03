# Repository Guidance

## Scope

These instructions apply to the entire repository. More specific guidance in a
nested `AGENTS.md` takes precedence only within that directory.

The maintained application lives under `code/`. Treat `code/IAMouflage/` and
`data/iam-dataset/` as external submodules: do not edit, reformat, or update them
unless the user explicitly requests it. Treat `code/artifacts/`, `code/runtime/`,
cache directories, and evaluation result directories as generated output.

## Working Principles

- Use the KISS principle. Prefer the smallest clear change that satisfies the
  requirement and preserves current behavior.
- Reuse existing models, services, repositories, error types, and utilities
  before adding another abstraction.
- Keep responsibilities narrow. Avoid speculative features, generic frameworks,
  hidden control flow, and premature optimization.
- Preserve deterministic behavior, explicit approval boundaries, immutable
  records, and version checks unless a requirement deliberately changes them.
- Do not modify unrelated code or generated data while completing a focused task.

## Language and Naming

- Use neutral, domain-agnostic terminology centered on authorization analysis,
  coverage validation, recommendation, evaluation, and controlled simulation.
- Keep new APIs, symbols, fixtures, logs, comments, and documentation factual and
  operational. Do not assign motives to capabilities or users.
- Preserve inherited external identifiers or source text only when compatibility
  or provenance requires it. Isolate that vocabulary at the integration boundary
  instead of spreading it into new application concepts.
- Choose descriptive names that communicate intent without implementation trivia.

## Python Standards

All new and modified Python must follow the Google Python Style Guide and PEP 8.
The repository requires Python 3.12 or newer and uses Ruff as the mechanical
source of truth.

- Prefer lines of at most 80 characters. Never exceed the configured 100-character
  limit unless an indivisible URL or external identifier requires it.
- Use `snake_case` for modules, functions, methods, and variables; `CapWords` for
  classes; and `UPPER_SNAKE_CASE` for constants.
- Add precise type annotations to new or changed function signatures. Prefer
  concrete types and modern syntax such as `str | None` and built-in generics.
- Keep imports at the top of the file, grouped as standard library, third-party,
  and local imports. Let Ruff sort them; do not use wildcard imports.
- Write concise Google-style docstrings for public modules, classes, functions,
  and methods when their purpose, arguments, return value, or raised exceptions
  are not self-evident. Comments should explain why, not restate the code.
- Prefer early returns and small functions over deep nesting. Use comprehensions
  only when they remain immediately readable.
- Catch specific exceptions. Preserve exception context with `raise ... from ...`
  when translating errors, and use the existing errors in `code/core/errors.py`
  where applicable.
- Avoid mutable default arguments, global mutable state, and untyped dictionaries
  when a current Pydantic model or a small typed model fits.
- Use `pathlib` for filesystem paths and timezone-aware UTC values for timestamps.
- Keep I/O at boundaries and core decision logic deterministic where practical.

## Architecture and Data

- Follow the current package boundaries documented in `README.md` and
  `code/README.md`.
- Extend the shared Pydantic contracts in `code/core/models.py` rather than
  passing loosely structured data between packages.
- Keep persisted formats backward compatible unless the task explicitly includes
  a migration. Maintain stable ordering and identifiers in serialized output.
- Never commit credentials or local configuration. Do not log tokens, keys, or
  other sensitive values; use opaque references at persistence boundaries.
- Validate external input at the boundary and return actionable, neutral errors.

## Tests and Verification

- Add or update tests for every behavior change and regression fix. Prefer small,
  deterministic tests with descriptive `test_<behavior>` names.
- Avoid network access, real external operations, time dependence, and shared
  mutable state in unit tests. Use the existing fakes and scripted providers.
- Run focused tests first, then the broadest relevant checks. From `code/`, the
  standard checks are:

```bash
.venv/bin/ruff check core ingestion environment validator proposer green_agent launchpad execution runner tests
.venv/bin/python -m pytest
```

- For evaluation components, also run the suite belonging to the changed area:

```bash
.venv/bin/python -m pytest evaluation-pipeline/tests -q
.venv/bin/python -m pytest evaluation-tests/tests -q
```

- If a check cannot run, report the exact reason and the closest verification
  performed. Do not claim success without evidence.

## Change Completion

Before finishing, review the diff for unnecessary complexity, naming consistency,
formatting, accidental generated files, and sensitive data. Update documentation
when commands, configuration, public behavior, or persisted formats change.
