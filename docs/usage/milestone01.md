# Milestone 1: Execution Foundation

This guide explains what Milestone 1 implemented and how to verify it manually.
Run all commands from `code/` unless a section says otherwise.

Milestone 1 does not add live Google Cloud actions. The only built-in execution
provider remains the deterministic simulator. Credential resolution is still
required because the simulator now follows the same guarded execution path that
future providers must use.

## What was implemented

### Opaque scenario credential references

Planner scenarios now require an opaque `credential_ref`:

```yaml
starting_service_account:
  identity: runner@authorized-project.iam.gserviceaccount.com
  credential_ref: run/default
  permissions:
    - storage.objects.get
```

The scenario contract rejects `access_token`, missing references, empty
references, and unknown fields. There is no compatibility path for scenarios
that embed tokens.

The runner requires a separate source descriptor:

```text
stdin
file:<path>
env:<name>
adc
impersonate:<principal>
```

A descriptor identifies where the execution service can resolve credential
material. It is not the credential itself. Raw credentials are not accepted as
command arguments.

### Credential resolution and leases

`CredentialResolver` binds an opaque reference to one typed source. It resolves
the source only after the static authorization checks pass and immediately
before the selected provider is called.

Every resulting `CredentialLease` contains a verified principal, expiry,
source kind, and runtime-only credential material. Resolution fails when:

- a reference is not registered;
- a supplied value is missing or empty;
- a file cannot be read or has group/other permissions;
- token metadata cannot be verified;
- the verified principal differs from the approved identity;
- the lease is expired or too close to expiry;
- ADC resolves service-account key credentials; or
- an impersonation target differs from the approved identity.

Lease representations redact credential material. Closing a lease clears its
credential reference, and service cleanup closes it after success, provider
failure, timeout, cancellation, or interruption.

### Typed execution contracts

The shared contracts now include:

- `ActionDefinition`, which names one registered catalog action and its concrete
  parameter model;
- `ExecutionSpec`, the immutable provider input bound to the approval, versions,
  identity, target, credential reference, timeout, and output limit;
- `ExecutionObservation`, the bounded provider-neutral result;
- `TechniqueActionParameters`, the current strict empty parameter contract; and
- `CredentialLease`, the runtime-only credential contract.

Current actions accept no parameters. Unknown fields, command strings, and
free-form arguments therefore fail Pydantic validation. The approval digest
binds the complete typed parameter payload.

Existing persisted execution records remain readable when they do not contain
the newly added `spec` field.

### Provider dispatch and one-time attempts

`ExecutionProvider` defines one provider-neutral method:

```text
execute(ExecutionSpec, CredentialLease) -> ExecutionObservation
```

The simulator implements this protocol without reading credential material.
Unknown or unavailable provider names fail during service assembly; they never
fall back to the simulator.

`ExecutionService` now applies one common path:

1. Enforce the global execution kill switch.
2. Load the registered approval and engagement authorization.
3. Verify the engagement, state version, matrix version, target, action,
   identity, typed arguments, validator result, and credential reference.
4. Resolve the registered action into an immutable `ExecutionSpec`.
5. Atomically reserve and consume the exact approval.
6. Resolve and verify a credential lease.
7. Invoke the configured provider once.
8. Validate that the observation matches the approved engagement, action,
   identity, and target.
9. Reject oversized observations or observations containing credential
   material.
10. Persist the execution and finalize the attempt.

Starting an attempt consumes the approval even if credential resolution or the
provider fails. A timeout or failure cannot be replayed without a new validation
and explicit approval. Attempt records contain fixed, non-sensitive failure
codes rather than provider error text.

## Setup

Create the development environment if it does not already exist:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[dev]'
```

Confirm that the protected data sources are available:

```bash
git -C .. submodule status
```

Do not edit the protected `IAMouflage/` or `../data/iam-dataset/` submodules.
Treat `artifacts/` and `runtime/` as generated output: inspect them when useful,
but do not manually modify or commit them.

## Manual offline checks

These checks do not require Gemini, Google credentials, or network access.

### 1. Inspect accepted credential-source descriptors

Run the parser directly:

```bash
.venv/bin/python - <<'PY'
from execution.credentials import parse_credential_source

sources = (
    "stdin",
    "file:/tmp/short-lived-token",
    "env:VERISENTINEL_TOKEN",
    "adc",
    "impersonate:runner@example.iam.gserviceaccount.com",
)
for value in sources:
    print(value.split(":", 1)[0], "->", parse_credential_source(value))
PY
```

Expected result: all five descriptors parse. File paths, environment names, and
impersonation principals appear as `[CONFIGURED]` in the source representation.

Now verify fail-closed parsing:

```bash
.venv/bin/python - <<'PY'
from execution.credentials import CredentialSourceError, parse_credential_source

invalid = ("", "raw-token-value", "file:", "env:NOT-VALID", "unknown:value")
for value in invalid:
    try:
        parse_credential_source(value)
    except CredentialSourceError as error:
        print(repr(value), "rejected:", error)
    else:
        raise SystemExit(f"unexpectedly accepted {value!r}")
PY
```

Expected result: every value is rejected with a generic source error.

### 2. Verify that the runner rejects a raw argument

Use only a synthetic value for this negative test:

```bash
.venv/bin/verisentinel run \
  --scenario=scenario.example.yaml \
  --credential-source=synthetic-value-that-is-not-a-source
```

Expected result:

```text
Input error: Credential source is unsupported or invalid.
```

The exit code is `2`, and the supplied synthetic value is not echoed. The
runner validates the descriptor before reading the scenario or loading Gemini.

### 3. Verify strict scenario rejection

Copy `scenario.example.yaml` to a temporary location and add this field beneath
`starting_service_account`:

```yaml
access_token: synthetic-token-must-be-rejected
```

Then run:

```bash
.venv/bin/verisentinel run \
  --scenario=/tmp/verisentinel-invalid-scenario.yaml \
  --credential-source=adc
```

Expected result: the command exits with code `2`, reports an unknown
`access_token` field, and does not echo its value.

Repeat the check after removing both `access_token` and `credential_ref`.
Expected result: scenario validation reports that `credential_ref` is required.

### 4. Exercise credential sources with deterministic fakes

The focused tests resolve synthetic `stdin`, `file`, and `env` values and use
offline fakes for ADC, impersonation, and token inspection:

```bash
.venv/bin/python -m pytest tests/unit/test_credentials.py -vv
```

Review the individual test names in the output. They cover source parsing,
protected-file permissions, missing files, principal checks, expiry checks,
ADC key rejection, impersonation binding, redacted errors, and lease cleanup.

### 5. Exercise provider dispatch and approval consumption

Run the execution tests with verbose names:

```bash
.venv/bin/python -m pytest tests/unit/test_execution.py -vv
```

Important checks to observe include:

- `test_execution_dispatches_typed_spec_through_provider`;
- `test_invalid_credential_consumes_approval_without_calling_provider`;
- `test_provider_failure_is_redacted_and_closes_lease`;
- `test_provider_timeout_consumes_approval_and_closes_lease`;
- `test_provider_interruption_closes_lease_and_records_failure`;
- `test_provider_observation_must_match_approved_specification`;
- `test_provider_observation_is_bounded`;
- `test_provider_cannot_return_credential_material`;
- `test_unavailable_provider_never_falls_back_to_simulator`; and
- `test_approval_reservation_is_atomic_across_repository_instances`.

These tests use a recording provider and synthetic credential inspector. They
also prove that approval mismatches, stale versions, disallowed targets,
unregistered actions, invalid parameters, and the kill switch stop execution
before the provider is invoked.

### 6. Verify that model input excludes credentials

Run the orchestration regression test:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_green_agent.py::test_proposal_model_input_excludes_credential_values \
  -vv
```

Expected result: the captured `ProposalRequest` contains neither the synthetic
credential value nor the opaque credential reference.

## Optional end-to-end simulator session

This check calls Gemini and resolves real short-lived Google credentials, but it
still does not perform a Google Cloud action. An approved action is handled by
the offline simulator.

1. Copy `scenario.example.yaml` to the ignored `scenario.yaml`.
2. Set the scenario identity to the exact principal that the credential source
   will resolve.
3. Configure a valid `GEMINI_API_KEY` in the ignored `.env` file.
4. Prefer ADC or service-account impersonation. Do not place a credential in the
   scenario or command line.
5. Start the planner:

```bash
.venv/bin/verisentinel run \
  --scenario=scenario.yaml \
  --credential-source=adc
```

For impersonation, use the exact scenario identity:

```bash
.venv/bin/verisentinel run \
  --scenario=scenario.yaml \
  --credential-source=impersonate:runner@project.iam.gserviceaccount.com
```

Expected behavior:

- the runner prints `Execution provider: simulator`;
- Gemini proposals still pass through deterministic validation;
- empty input never implies approval;
- only an explicitly selected candidate reaches execution;
- the candidate is revalidated immediately before approval registration;
- one approval produces at most one provider attempt; and
- an approved simulation creates a new environment-state version.

Press `Ctrl-C` before approval to verify that interruption returns exit code
`130` without implicit execution.

## Inspect persisted records

After an approved optional simulator run, inspect only the non-sensitive fields:

```bash
find runtime/execution -maxdepth 2 -type f -name '*.json' -print
```

Attempt records are stored under:

```text
runtime/execution/attempts/
runtime/execution/attempts-by-approval/
```

A successful attempt contains an opaque attempt ID, approval ID, engagement ID,
provider name, timestamps, `succeeded` status, and execution ID. A failed
attempt contains a fixed failure code instead of provider error text.

Execution records retain the typed request, exact approval, typed spec, and
bounded observation. The only credential-related persisted value is the opaque
`credential_ref`.

Search for prohibited credential field names without placing an actual
credential in the command line:

```bash
rg -n '"(access_token|_access_token|refresh_token|authorization)"' \
  runtime/execution runtime/traces
```

Expected result: no persisted credential field is found. Trace records may
contain non-secret model token-usage counters such as `total_tokens`; those are
usage metrics, not credential material.

## Full verification

Run the maintained application checks:

```bash
.venv/bin/ruff check \
  core ingestion environment validator proposer green_agent \
  launchpad execution runner tests
.venv/bin/python -m pytest
```

At the Milestone 1 checkpoint, Ruff passes and the main suite reports 106
passing tests.

The evaluation harnesses are separate from the maintained execution path:

```bash
.venv/bin/python -m pytest evaluation-pipeline/tests -q
.venv/bin/python -m pytest evaluation-tests/tests -q
```

Some evaluation tests require ignored local fixtures: an
`evaluation-pipeline/scenario.yaml` and frozen reports under
`evaluation-pipeline/runs/`. If those files are absent, report the missing
fixture rather than treating it as an execution-foundation failure or generating
new evaluation output during this check.

## Security properties to confirm

Before accepting a manual test run, confirm all of the following:

- no scenario contains `access_token` or another raw credential field;
- no command argument contains a credential value;
- the credential reference is opaque and matches the approved request;
- the resolved principal exactly matches the requested execution identity;
- failed or timed-out attempts cannot reuse the approval;
- provider errors returned to the caller are neutral and bounded;
- provider output cannot change the approved identity, action, or target;
- runtime JSON and traces contain no credential material; and
- the simulator remains the only built-in provider.

## Intentionally not implemented

Milestone 1 does not include:

- the Typer command hierarchy planned for Milestone 2;
- the local container capsule planned for Milestone 3;
- a live Google Cloud execution provider;
- arbitrary command or free-form parameter execution;
- the optional DeepSeek copilot adapter planned for Milestone 4; or
- managed remote execution.

Unknown provider configuration continues to fail closed. Do not interpret the
provider protocol as evidence that a live provider is available.

## Milestone commits

The implementation was delivered through these independently reversible
commits:

```text
69e4ce8 replace(runner): require credential references in scenario files
17b299f add(execution): resolve credentials through typed sources
5eb239c add(execution): define typed action and execution contracts
9b47892 test(execution): cover unreadable credential source handling
0845d29 refactor(execution): dispatch requests through a provider protocol
c3d0eb5 document(execution): record milestone one completion
f558e08 test(execution): cover provider assembly and legacy records
5f92b9d clarify(execution): describe transient credential resolution accurately
```
