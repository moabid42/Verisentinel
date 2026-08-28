locals {
  service_account_id = split("@", var.starting_principal)[0]
  expected_principal = "${local.service_account_id}@${var.project_id}.iam.gserviceaccount.com"
}

resource "google_service_account" "runner" {
  project      = var.project_id
  account_id   = local.service_account_id
  display_name = "Verisentinel GCS flag scenario runner"
  description  = "Scenario-owned identity for guarded GCS action delivery."

  lifecycle {
    precondition {
      condition     = var.starting_principal == local.expected_principal
      error_message = "starting_principal must belong to project_id."
    }
  }
}

resource "google_service_account_iam_member" "provisioner_impersonation" {
  service_account_id = google_service_account.runner.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = var.provisioner_member
}

resource "google_storage_bucket" "action_sink" {
  project                     = var.project_id
  name                        = var.bucket_name
  location                    = var.location
  force_destroy               = true
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 1
    }
  }

  soft_delete_policy {
    retention_duration_seconds = 0
  }
}

# This binding is authoritative for objectCreator on the scenario bucket. No
# operator, default identity, or other service account receives this role here.
resource "google_storage_bucket_iam_binding" "scenario_writer" {
  bucket = google_storage_bucket.action_sink.name
  role   = "roles/storage.objectCreator"
  members = [
    "serviceAccount:${google_service_account.runner.email}",
  ]
}
