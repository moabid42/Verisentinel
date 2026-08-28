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

- target: `gs://verisentinel-flag-banded-charmer-485112-q5`;
- starting identity:
  `verisentinel-flag-runner@banded-charmer-485112-q5.iam.gserviceaccount.com`;
- effective permission: `storage.objects.create`;
- intended registered technique:
  `unauthenticated-access:gcp-public-buckets-privilege-escalation:6`; and
- detection profile: Sigma, which does not cover the selected permission in the
  current corpus.

The scenario contains only an opaque `credential_ref`. Tokens and model keys
must not be added to this directory.

## One-time GCP preparation

Run commands from `code/`. Authenticate the operator account named in the
scenario and refresh Application Default Credentials:

```bash
gcloud auth login mouad.abid@lyraix.ai
gcloud auth application-default login mouad.abid@lyraix.ai
gcloud config set project banded-charmer-485112-q5
```

Create the scenario service account if it does not already exist:

```bash
gcloud iam service-accounts create verisentinel-flag-runner \
  --project=banded-charmer-485112-q5 \
  --display-name='Verisentinel flag scenario runner'
```

The authenticated operator must be able to create the bucket, update its IAM
policy, and list and read its action objects. It must also be allowed to
impersonate the scenario service account. Grant the narrow service-account
binding when needed:

```bash
gcloud iam service-accounts add-iam-policy-binding \
  verisentinel-flag-runner@banded-charmer-485112-q5.iam.gserviceaccount.com \
  --project=banded-charmer-485112-q5 \
  --member='user:mouad.abid@lyraix.ai' \
  --role='roles/iam.serviceAccountTokenCreator'
```

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
declared private bucket:

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
It verifies object-creation access before persisting the non-sensitive
connection record.

## Run and approve

Build the scenario's Sigma-only snapshot during the first run:

```bash
.venv/bin/verisentinel --dev scenario run \
  scenarios/gcs_action_delivery_flag/scenario.yaml \
  --credential-source \
  impersonate:verisentinel-flag-runner@banded-charmer-485112-q5.iam.gserviceaccount.com \
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
  gs://verisentinel-flag-banded-charmer-485112-q5/actions/
```

Copy the object from the completed run to a temporary file, replacing the URI
with the exact object shown by the previous command:

```bash
gcloud storage cp \
  gs://verisentinel-flag-banded-charmer-485112-q5/actions/approval_REPLACE.json \
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
  gs://verisentinel-flag-banded-charmer-485112-q5
```

This final command is destructive and should be run only after confirming the
exact bucket name.
