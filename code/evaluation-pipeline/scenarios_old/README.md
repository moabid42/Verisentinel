# Preserved inline (Mode A) scenarios

Kept copies of the self-contained scenarios that carry their own `techniques:` and `detections:`
blocks, so they are not lost as `scenarios/` moves toward IAMouflage (Mode B) sourcing. These are
**copies** — the originals still live in `../scenario.yaml` and `../scenarios/`.

This folder is a sibling of `scenarios/`, so `run.py --all` does **not** pick these up. Run one
explicitly with `run.py --scenario scenarios_old/<name>.yaml`.

| File | Story |
|---|---|
| `sigma-gap-cloudbuild-actas.yaml` | The original real inline scenario (`../scenario.yaml`): CI/CD deployer → Cloud Build actAs → serial-port secret. |
| `cloudfunctions-actas-secret-exfil.yaml` | Deploy a function as a privileged runtime SA → Secret Manager → BigQuery exfil. |
| `gke-workload-pivot-metadata.yaml` | Read-only GKE foothold → exec into a privileged pod → node metadata → secret. |
| `artifactregistry-supply-chain-exfil.yaml` | Poison an image a Cloud Run service consumes → actAs redeploy → GCS secret → BigQuery. |
