# Milestone 2: Unified Typer CLI

This guide explains what Milestone 2 implemented and how to test it manually.
Run application commands from `code/` unless a section says otherwise.

Milestone 2 changed the command boundary, not the planner's authority model. At
that milestone boundary, the deterministic simulator was the only built-in
execution provider. The direct workflow remains human-gated; Milestone 3 has
since added the optional local capsule described in
[`milestone03.md`](milestone03.md).

## What was implemented

### One installed command application

The package now installs one `verisentinel` Typer application with this initial
surface:

```text
verisentinel scenario validate PATH
verisentinel auth inspect --credential-source SOURCE
verisentinel corpus build
verisentinel corpus status
verisentinel sandbox build
verisentinel sandbox doctor
verisentinel run --scenario PATH --credential-source SOURCE
verisentinel scenario run PATH --credential-source SOURCE
```

The maintained runner and ingestion entrypoints no longer construct independent
`argparse` parsers. `python -m ingestion` remains a compatibility path to
`verisentinel corpus build`. `python run.py` opens the unified command
application, so a direct workflow now requires the `run` subcommand.

Typer is declared as the bounded dependency `typer>=0.16,<1`, and the package
exposes the console script through `pyproject.toml`.

### Application-service boundary

The command handlers parse command input, invoke functions in
`runner/application.py`, render bounded output, and translate failures to process
exit codes. The application services own:

- standalone scenario validation;
- non-sensitive authentication inspection;
- corpus build and current-status queries;
- sandbox build and readiness inspection; and
- assembly of the existing interactive planner workflow.

Scenario validation, credential resolution, corpus construction, deterministic
orchestration, approval enforcement, and execution policy remain in their
owning services.

### Non-sensitive authentication inspection

`CredentialResolver.inspect` resolves a source briefly, verifies its principal
and expiry, copies only public metadata into a `CredentialInspection`, and
closes the runtime lease in guaranteed cleanup.

The command prints only:

- source kind;
- verified principal; and
- expiry timestamp.

It does not print the source locator, opaque credential reference, access token,
or refreshed credential object. The same expiry and source-security checks used
by execution apply to inspection.

### Stable exit behavior

Commands use these process exit codes:

| Code | Meaning |
| --- | --- |
| `0` | The requested command completed successfully. |
| `1` | An application operation failed. |
| `2` | Command syntax, user input, or local configuration is invalid. |
| `130` | The process was interrupted. |

An interrupt prints that no implicit approval or execution occurred. It never
converts missing operator input into a decision.

### Bounded diagnostics

Command-generated values and known error messages are truncated to a fixed
maximum before rendering. Unexpected exception messages are not printed. Help
text contains only static command descriptions and never reads `.env`, a
scenario, a credential source, or local runtime state.

At the Milestone 2 boundary, `sandbox doctor` reported that the capsule was not
installed. It now performs the readiness checks documented in the Milestone 3
guide.

## Setup

Create or refresh the editable development environment:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[dev]'
```

Confirm that the command was installed:

```bash
.venv/bin/verisentinel --help
```

Expected result: the help lists `scenario`, `auth`, `corpus`, `sandbox`, and
`run`. It must not display credential values, environment-variable values,
scenario contents, or local paths.

## Manual offline checks

The checks in this section do not require Gemini, Google credentials, or network
access unless explicitly stated.

### 1. Inspect all help routes

```bash
.venv/bin/verisentinel --help
.venv/bin/verisentinel scenario --help
.venv/bin/verisentinel scenario validate --help
.venv/bin/verisentinel scenario run --help
.venv/bin/verisentinel auth --help
.venv/bin/verisentinel auth inspect --help
.venv/bin/verisentinel corpus --help
.venv/bin/verisentinel corpus build --help
.venv/bin/verisentinel corpus status --help
.venv/bin/verisentinel sandbox --help
.venv/bin/verisentinel sandbox build --help
.venv/bin/verisentinel sandbox doctor --help
.venv/bin/verisentinel run --help
```

Expected result: every command exits with `0`; required arguments and options
are visible; no command loads `.env` or attempts credential resolution.

### 2. Validate a scenario without starting a run

```bash
.venv/bin/verisentinel scenario validate scenario.example.yaml
```

Expected result: a `SCENARIO VALID` panel containing the scenario name and
target scope.

The command must not build a corpus, load Gemini, create runtime records, prompt
for a decision, or resolve credentials.

Test invalid input:

```bash
set +e
.venv/bin/verisentinel scenario validate /tmp/missing-scenario.yaml
status=$?
set -e
test "$status" -eq 2
```

Expected result: a bounded `Input error` is written and the exit code is `2`.

### 3. Reject a raw credential argument

Use only a synthetic value for this negative test:

```bash
set +e
.venv/bin/verisentinel auth inspect \
  --credential-source=synthetic-value-that-is-not-a-source
status=$?
set -e
test "$status" -eq 2
```

Expected output:

```text
Input error: Credential source is unsupported or invalid.
```

The synthetic value must not be echoed.

### 4. Inspect a configured credential source

This optional check resolves real short-lived credentials and may contact Google
authentication endpoints. Prefer Application Default Credentials or
impersonation; do not put a token directly in the command.

```bash
.venv/bin/verisentinel auth inspect --credential-source=adc
```

For impersonation:

```bash
.venv/bin/verisentinel auth inspect \
  --credential-source=impersonate:runner@project.iam.gserviceaccount.com
```

Expected output has exactly this shape:

```text
Source kind: adc
Principal: verified-principal@example.com
Expires at: 2026-08-26T12:34:56+00:00
```

Values vary by credential. Verify that the output does not include an access
token, file path, environment-variable name, credential reference, or serialized
Google credential object.

### 5. Build and inspect the coverage corpus

The build requires initialized IAMouflage and IAM dataset submodules:

```bash
.venv/bin/verisentinel corpus build
.venv/bin/verisentinel corpus status
```

Both commands should report:

```text
Status: available
Matrix version: sha256:...
Permissions: ...
Detections: ...
Techniques: ...
```

`corpus status` is read-only. If no corpus has been built in the configured
artifact directory, it instead prints `Status: not built` and exits with `0`.

The compatibility command should reach the same build route:

```bash
.venv/bin/python -m ingestion --help
```

Expected result: help for `verisentinel corpus build`, not an `argparse` parser.

### 6. Inspect sandbox readiness

```bash
.venv/bin/verisentinel sandbox doctor
```

The command now prints one result for each required isolation capability. Before
the locked image and internal network are installed, expect `Status: unavailable`
and one or more failed checks. After Milestone 3 setup, expect `Status: available`
and every check to pass. See [`milestone03.md`](milestone03.md) for the exact
setup and review criteria.

This diagnostic remains read-only and exits with `0`; readiness is reported in
the content rather than as a command failure.

### 7. Verify required-input exit codes

```bash
set +e
.venv/bin/verisentinel scenario validate
scenario_status=$?
.venv/bin/verisentinel auth inspect
auth_status=$?
.venv/bin/verisentinel run --scenario=scenario.example.yaml
run_status=$?
set -e
test "$scenario_status" -eq 2
test "$auth_status" -eq 2
test "$run_status" -eq 2
```

Expected result: Typer prints usage guidance and every incomplete command exits
with `2`.

## Optional direct planner session

This check calls Gemini and resolves real short-lived Google credentials, but an
approved action still goes only to the deterministic simulator.

1. Copy `scenario.example.yaml` to the ignored `scenario.yaml`.
2. Set the scenario identity to the principal resolved by the credential source.
3. Configure `GEMINI_API_KEY` in the ignored `.env` file.
4. Start the scenario from the terminal launchpad:

```bash
.venv/bin/verisentinel scenario run scenario.yaml \
  --credential-source=adc
```

Expected behavior is unchanged from Milestone 1:

- the active run panel reports `Provider  simulator`;
- Gemini can rank only cataloged techniques;
- deterministic validation controls which candidates are displayed;
- no more than three candidates are displayed;
- empty or malformed input never implies approval;
- only an explicitly approved candidate reaches execution; and
- one approval permits at most one provider attempt.

To verify interruption behavior, press `Ctrl+C` before submitting an operator
decision and then inspect the shell status:

```bash
echo $?
```

Expected result: the exit code is `130`, the command states that no implicit
approval or execution occurred, and no new approval or execution record exists
for the interrupted prompt.

## Automated verification

Run the focused CLI and credential checks first:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_runner.py \
  tests/unit/test_runner_application.py \
  tests/unit/test_credentials.py -q
```

Run repository lint and the complete main suite:

```bash
.venv/bin/ruff check \
  core ingestion environment validator proposer \
  green_agent launchpad execution runner tests
.venv/bin/python -m pytest
```

Run the isolated evaluation suites when their local scenario and generated
report fixtures are present:

```bash
.venv/bin/python -m pytest evaluation-pipeline/tests -q
.venv/bin/python -m pytest evaluation-tests/tests -q
```

## Security review checklist

After any CLI change, verify all of the following:

- help output contains no local configuration or credential value;
- raw values are rejected as credential-source descriptors and are not echoed;
- `auth inspect` prints only source kind, verified principal, and expiry;
- scenario validation creates no runtime or artifact records;
- unknown exception messages are not rendered;
- output derived from external or persisted values is bounded;
- interrupt exit `130` never records a decision or implies approval; and
- generated `artifacts/` and `runtime/` files remain unstaged.

## Not implemented at the Milestone 2 boundary

Milestone 2 does not add:

- a local container execution capsule;
- live Google Cloud action execution;
- session list, show, resume, or trace-export commands;
- the DeepSeek copilot adapter;
- a managed execution runtime; or
- an alternate authorization or approval path.

The local capsule was subsequently delivered in Milestone 3. The remaining
items are governed by Milestones 4 and 5 in the architecture roadmap.
