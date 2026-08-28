# GCS action-delivery flag scenario

This scenario exercises the maintained planner, explicit operator approval,
one-time execution authorization, local capsule, fixed GCP gateway, and a real
scenario bucket. It does not claim that a catalog technique's external effects
occurred. The current gateway's supported effect is delivery of the approved
typed action envelope.

The goal is achieved when the envelope is retrieved from the declared bucket
and `verify_flag.py` prints a run-specific value in this form:

```text
FLAG{gcs-action-delivery:approval_<32 lowercase hexadecimal characters>}
```

The approval identifier is generated only after an explicit operator decision.
It is included in the object written by the fixed gateway, so obtaining the flag
demonstrates that the guarded execution path reached the scenario target.

## Scenario contract

The scenario deliberately selects one narrow path supported by the current
implementation:

- target: `gs://verisentinel-tf-flag-project-551b0c2b-9622-4479-b23`;
- starting identity:
  `verisentinel-tf-flag-runner@project-551b0c2b-9622-4479-b23.iam.gserviceaccount.com`;
- effective permission: `storage.objects.create`;
- intended registered technique:
  `unauthenticated-access:gcp-public-buckets-privilege-escalation:6`; and
- detection profile: Sigma, which does not cover the selected permission in the
  current corpus.

The scenario contains only an opaque `credential_ref`. Its `terraform_root`
points to the reviewed and provider-locked module in `terraform/`. Tokens,
Terraform state, plan files, and model keys must not be added to this directory.

Terraform creates all scenario-owned cloud resources:

- the service account declared in `starting_service_account.identity`;
- the private bucket declared in `infrastructure.path`;
- an authoritative bucket-level `roles/storage.objectCreator` binding whose
  only member is that service account; and
- a service-account-level `roles/iam.serviceAccountTokenCreator` grant for the
  verified ADC principal that performed provisioning.

The provisioning principal is a control-plane identity used only by
`infra create`. Sandbox connection and execution resolve a fresh impersonated
token and reject it unless its principal exactly equals the scenario service
account.

## One-time GCP preparation

Run commands from `code/`. Install Terraform 1.10 or newer either on `PATH` or
beside the virtual-environment Python as `.venv/bin/terraform`. The module pins
Google provider 7.43.0 in `.terraform.lock.hcl`.

Authenticate the operator account named in the scenario and refresh Application
Default Credentials:

```bash
gcloud auth login mouadabid2002@gmail.com
gcloud config set account mouadabid2002@gmail.com
gcloud config set project project-551b0c2b-9622-4479-b23
gcloud auth application-default login mouadabid2002@gmail.com \
  --project=project-551b0c2b-9622-4479-b23
gcloud auth application-default set-quota-project \
  project-551b0c2b-9622-4479-b23
```

The authenticated operator must be allowed to create service accounts and
buckets and update their IAM policies. Terraform creates the scenario service
account and its impersonation grant; do not create either manually.

Add `GEMINI_API_KEY` to the ignored `code/.env`; do not place it in the scenario
or pass it on the command line.

## Validate and prepare the runtime

```bash
.venv/bin/verisentinel scenario validate \
  scenarios/gcs_action_delivery_flag/scenario.yaml
.venv/bin/verisentinel sandbox build
.venv/bin/verisentinel sandbox doctor
```

Expected results are `SCENARIO VALID` and an available sandbox. Provision the
declared service account, private bucket, and narrow IAM bindings:

```bash
.venv/bin/verisentinel --dev infra create \
  --scenario scenarios/gcs_action_delivery_flag/scenario.yaml \
  --location EU
```

Record the returned `infra_<hex>` identifier, then connect the capsule using the
scenario service account:

```bash
.venv/bin/verisentinel --dev sandbox connect infra_REPLACE_WITH_RETURNED_ID \
  --scenario scenarios/gcs_action_delivery_flag/scenario.yaml
```

The development connection defaults to the exact required impersonation source.
It verifies the resolved principal and object-creation access before persisting
the non-sensitive connection record. Do not override it with operator ADC; a
principal mismatch fails closed.

## Run and approve

Build the scenario's Sigma-only snapshot during the first run:

```bash
.venv/bin/verisentinel --dev scenario run \
  scenarios/gcs_action_delivery_flag/scenario.yaml \
  --credential-source \
  impersonate:verisentinel-tf-flag-runner@project-551b0c2b-9622-4479-b23.iam.gserviceaccount.com \
  --rebuild-snapshot
```

Review the candidates. Approve only the card whose technique is
`unauthenticated-access:gcp-public-buckets-privilege-escalation:6` and whose
required permission is `storage.objects.create`. Do not approve a different
candidate merely to advance the run. After the terminal reports successful
delivery and begins another proposal cycle, enter `q` to terminate cleanly.

## Retrieve and verify the flag

List the action objects with the operator credential:

```bash
gcloud storage ls \
  gs://verisentinel-tf-flag-project-551b0c2b-9622-4479-b23/actions/
```

Copy the object from the completed run to a temporary file, replacing the URI
with the exact object shown by the previous command:

```bash
gcloud storage cp \
  gs://verisentinel-tf-flag-project-551b0c2b-9622-4479-b23/actions/approval_REPLACE.json \
  /tmp/verisentinel-action-envelope.json
```

Verify the downloaded envelope:

```bash
.venv/bin/python scenarios/gcs_action_delivery_flag/verify_flag.py \
  /tmp/verisentinel-action-envelope.json
```

Success prints exactly one `FLAG{gcs-action-delivery:approval_<hex>}` value.
The verifier rejects malformed envelopes, a different scenario identity or
target, another action, and invalid approval or engagement identifiers.

## Cleanup

The bucket has a one-day object lifecycle, but the bucket itself is not removed
automatically. After retaining any evidence needed for the test, delete it with
the operator credential:

```bash
gcloud storage rm --recursive \
  gs://verisentinel-tf-flag-project-551b0c2b-9622-4479-b23
```

Then delete the scenario-owned service account if it is no longer needed:

```bash
gcloud iam service-accounts delete \
  verisentinel-tf-flag-runner@project-551b0c2b-9622-4479-b23.iam.gserviceaccount.com \
  --project=project-551b0c2b-9622-4479-b23
```

These commands are destructive and should be run only after confirming the
exact resource names. Manual deletion also leaves the ignored local Terraform
state stale; remove that generated deployment state before recreating the same
scenario.
