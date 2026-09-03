# Milestone 3: Local Execution Capsule

This guide describes the Milestone 3 implementation and how to test it manually.
Run commands from `code/` unless a section says otherwise. The capsule is an
optional execution provider; the deterministic simulator remains the default.

## What was implemented

### Fixed typed-operation image

The repository contains `execution/capsule/Dockerfile`, a fixed Python
entrypoint, and a bootstrap `execution/capsule/image.lock`. A local build writes
its exact immutable image reference to the generated
`runtime/capsule/image.lock`. The image:

- uses a base image pinned by digest and is selected through the active digest
  lock;
- declares a non-root user, one fixed entrypoint, and no selectable command;
- accepts only one strict `ExecutionSpec` and one credential file at fixed,
  read-only paths;
- recognizes only the registered `catalog.technique` operation with the
  `technique.none.v1` parameter model; and
- contacts only `http://verisentinel-mock:8080/execute`.

Unknown fields, arbitrary commands, unregistered operations, free-form
parameters, malformed endpoint output, and oversized output fail closed.

### Bounded Docker provider

`CapsuleExecutionProvider` prepares a mode-`0400` specification and credential
inside a mode-`0700` temporary directory immediately before invocation. It
starts Docker without a shell or inherited standard input and applies:

- an internal, explicitly selected Docker network;
- a read-only root filesystem and two fixed read-only input mounts;
- the calling non-root user and group;
- all-capability removal, no-new-privileges, and a 64-file descriptor limit;
- 0.5 CPU, 128 MiB memory and swap, 32 processes, and a 16 MiB temporary
  filesystem;
- the approved specification timeout and output limit; and
- an image reference pinned by the active digest lock with pulling disabled.

The provider always removes the named container and temporary input directory.
Timeouts, output overflow, endpoint errors, malformed output, non-zero exit,
runtime failure, and cleanup failure become bounded neutral errors. Raw Docker
diagnostics and credential values are not returned.

### Read-only readiness diagnostics

`verisentinel sandbox doctor` inspects the Docker server, Linux cgroups and
seccomp, locked image identity, image entrypoint and user, internal network, and
the configured isolation contracts. It does not start a container or make an
endpoint request. Selecting `EXECUTION_PROVIDER=capsule` repeats this readiness
gate during application assembly and does not fall back to the simulator.

### Approval-preserving integration

The capsule uses the same execution-service path as the simulator: target and
version checks, exact approval binding, atomic attempt reservation, immediate
credential resolution, provider invocation, observation validation, and attempt
finalization. Once invocation starts, success or failure consumes that approval.
The kill switch still applies before provider invocation.

### Reviewed local measurement

One successful local integration run produced the following diagnostic sample:

| Measurement | Observed value |
| --- | ---: |
| Container startup latency | about 50 ms |
| Total provider duration | about 797 ms |
| Peak container memory | about 15.9 MiB |
| Cleanup duration | about 24 ms |

The sample used Docker 29.7.2 on Linux x86-64. The mock endpoint deliberately
waited 200 ms before replying, so total duration includes that delay. This is a
single development-machine observation, not a benchmark or managed-runtime
selection result.

## Prerequisites

- Python 3.12 and the editable development environment from the main README;
- a Linux Docker daemon available to the current non-root user;
- cgroups and Docker's default seccomp profile; and
- the pinned Python base image available locally.

Confirm the basic environment:

```bash
test "$(id -u)" -ne 0
docker version
docker info --format '{{json .SecurityOptions}} {{.CgroupVersion}} {{.OSType}}'
```

If the base image is absent and network access is allowed, fetch that exact
digest once:

```bash
docker pull 'python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7'
```

## Build the locked sandbox

Build the image and create the internal network through the maintained CLI:

```bash
.venv/bin/verisentinel sandbox build
```

The command builds without network access, base-image pulling, or provenance
metadata. It records the exact resulting image ID in the generated runtime lock
and fails if an existing network is not internal. Docker build timestamps mean
that separate valid local builds are not expected to reproduce one historical
image ID.

Verify that the result resolves through the active runtime lock and has the
fixed image configuration:

```bash
capsule_image="$(tr -d '\n' < runtime/capsule/image.lock)"
docker image inspect "$capsule_image" --format '{{.Id}}'
docker image inspect "$capsule_image" \
  --format 'user={{.Config.User}} entrypoint={{json .Config.Entrypoint}} cmd={{json (index .Config "Cmd")}}'
```

Expected results:

- the image ID is the `sha256:` value in `runtime/capsule/image.lock`;
- the declared user is `65532:65532`;
- the entrypoint is
  `["/usr/local/bin/python","/opt/verisentinel/entrypoint.py"]`; and
- the command is empty.

## Run the doctor

Inspect the resources prepared by `sandbox build`:

```bash
docker network inspect verisentinel-capsule \
  --format 'internal={{.Internal}} driver={{.Driver}}'
.venv/bin/verisentinel sandbox doctor
```

Expected result: the network prints `internal=true`; the doctor prints
`SANDBOX READY`, `Provider  capsule`, and `PASS` for all of these checks:

```text
runtime
host-security
image-digest
fixed-entrypoint
non-root-image
internal-network
filesystem-isolation
process-isolation
credential-isolation
fixed-endpoint
```

The doctor intentionally exits with `0` even when readiness is unavailable.
Review `Status` and every check instead of relying only on the exit code.

## Manually exercise the isolated entrypoint

The following check uses only a synthetic token and the repository's private
mock endpoint. It does not contact Google Cloud or any public endpoint.

Create temporary inputs:

```bash
capsule_inputs="$(mktemp -d)"
chmod 700 "$capsule_inputs"
printf '%s' 'synthetic-manual-token' > "$capsule_inputs/credential"
chmod 400 "$capsule_inputs/credential"
cp "$capsule_inputs/credential" "$capsule_inputs/expected-token"
chmod 400 "$capsule_inputs/expected-token"
cat > "$capsule_inputs/spec.json" <<'JSON'
{
  "engagement_id": "manual-engagement",
  "candidate_id": "manual-candidate",
  "action": {
    "action_id": "technique:manual",
    "provider_operation": "catalog.technique",
    "parameter_model": "technique.none.v1",
    "technique_id": "manual",
    "observed_permission_footprint": [],
    "expected_capabilities": []
  },
  "identity": "operator@example.test",
  "target": "projects/sandbox",
  "arguments": {},
  "validator_result_id": "manual-validation",
  "approval_id": "manual-approval",
  "state_version": "manual-state",
  "matrix_version": "manual-matrix",
  "credential_ref": "manual/default",
  "timeout_seconds": 5.0,
  "output_limit_bytes": 65536
}
JSON
chmod 400 "$capsule_inputs/spec.json"
```

Start the fixed mock endpoint on the internal network:

```bash
docker run --detach --rm \
  --name verisentinel-mock-manual \
  --network verisentinel-capsule \
  --network-alias verisentinel-mock \
  --read-only \
  --user "$(id -u):$(id -g)" \
  --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --pids-limit 32 \
  --memory 67108864 --memory-swap 67108864 --cpus 0.25 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8388608,mode=1777 \
  --mount "type=bind,src=$PWD/tests/fixtures/capsule_mock_endpoint.py,dst=/tmp/mock_endpoint.py,readonly" \
  --mount "type=bind,src=$capsule_inputs/expected-token,dst=/tmp/expected-token,readonly" \
  'python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7' \
  /usr/local/bin/python /tmp/mock_endpoint.py
```

Run the capsule with the same isolation flags enforced by the provider:

```bash
docker run --rm \
  --name verisentinel-capsule-manual \
  --pull never \
  --network verisentinel-capsule \
  --read-only \
  --user "$(id -u):$(id -g)" \
  --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --pids-limit 32 \
  --memory 134217728 --memory-swap 134217728 --cpus 0.5 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777 \
  --ulimit nofile=64:64 \
  --mount "type=bind,src=$capsule_inputs/spec.json,dst=/run/verisentinel/spec.json,readonly" \
  --mount "type=bind,src=$capsule_inputs/credential,dst=/run/verisentinel/credential,readonly" \
  "$capsule_image"
```

Expected result: one JSON `ExecutionObservation` with `success: true`,
`execution_id: capsule-manual-approval`, and the approved identity, action, and
target. The synthetic token must not appear in the JSON or Docker arguments.

Stop the endpoint and repeat the capsule command to inspect a bounded failure:

```bash
docker stop verisentinel-mock-manual
set +e
# Repeat the preceding capsule command.
capsule_status=$?
set -e
test "$capsule_status" -ne 0
```

Expected stderr is only `capsule_error:endpoint_failed`; it must not contain the
token, request, local mount source, or Docker diagnostic text.

Remove the manual inputs and optional network when finished:

```bash
docker rm -f verisentinel-mock-manual verisentinel-capsule-manual \
  >/dev/null 2>&1 || true
rm -rf -- "$capsule_inputs"
docker network rm verisentinel-capsule
```

The final network command is optional if the capsule will be used again. The
temporary path is the exact directory returned by `mktemp`; do not substitute a
broad path.

## Test the complete approved execution path

The Docker integration test is the reproducible full-path check. It builds the
locked image offline, creates a unique internal network, starts the private mock,
constructs a valid approval and synthetic credential lease, invokes
`ExecutionService`, validates the typed observation, and checks approval
consumption and cleanup:

```bash
.venv/bin/python -m pytest tests/integration/test_capsule_integration.py -q
```

Expected result: two passing tests. The second test deliberately omits the
endpoint and proves that the failed invocation still consumes the approval and
removes the capsule and temporary credential input. The file skips rather than
pulling an image when Docker, non-root access, or the pinned base image is
unavailable.

After the run, check for leaked resources and credential text:

```bash
test -z "$(docker ps -aq --filter name=verisentinel-capsule-)"
test -z "$(docker network ls -q --filter name=verisentinel-test-)"
test -z "$(docker network ls -q --filter name=verisentinel-failure-)"
! rg --hidden --glob '*.json' 'synthetic-capsule-integration-token' runtime
```

The final command also succeeds when `runtime/` does not exist.

## Focused automated checks

Run the capsule, execution-service, and CLI diagnostics coverage:

```bash
.venv/bin/ruff check \
  execution/capsule \
  tests/unit/test_capsule_entrypoint.py \
  tests/unit/test_capsule_provider.py \
  tests/unit/test_capsule_doctor.py \
  tests/integration/test_capsule_integration.py
.venv/bin/python -m pytest \
  tests/unit/test_capsule_entrypoint.py \
  tests/unit/test_capsule_provider.py \
  tests/unit/test_capsule_doctor.py \
  tests/unit/test_execution.py \
  tests/unit/test_runner.py \
  tests/unit/test_runner_application.py \
  tests/integration/test_capsule_integration.py -q
```

For the complete repository verification:

```bash
.venv/bin/ruff check core ingestion environment validator proposer green_agent launchpad execution runner tests
.venv/bin/python -m pytest
.venv/bin/python -m pytest evaluation-pipeline/tests -q
.venv/bin/python -m pytest evaluation-tests/tests -q
```

The evaluation suites require their separately managed local scenario and frozen
report fixtures. If those ignored inputs are absent, report the missing fixture;
do not generate or commit evaluation output as part of this milestone check.

## Security review checklist

Before accepting a capsule change, confirm all of the following:

- the configured image exactly matches the generated
  `runtime/capsule/image.lock`, or the tracked bootstrap lock before a local
  build;
- the selected network is internal and the fixed mock is its only endpoint;
- no command follows the image reference in provider-generated arguments;
- neither the repository root nor `.env` is mounted into the capsule;
- the credential value appears only in the temporary fixed-path file;
- temporary inputs use modes `0700` and `0400` and are absent after every exit;
- no capsule container remains after success, failure, timeout, output overflow,
  cleanup handling, cancellation, or interruption;
- persisted JSON, traces, errors, process arguments, and model context contain no
  credential value;
- approval, target, action, identity, state version, matrix version, output, and
  timeout bindings remain unchanged at provider dispatch; and
- selecting an unavailable capsule fails assembly without simulator fallback.

## Intentionally not implemented

Milestone 3 does not add live Google Cloud operations, public network access,
arbitrary command execution, repository mounts, model tools, the optional
DeepSeek copilot adapter, or a managed execution runtime. The capsule is a local
mock-only measurement and isolation boundary, not a production sandbox claim.
