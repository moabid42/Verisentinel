output "infrastructure_path" {
  description = "Canonical scenario target checked by the runner."
  value       = "projects/${var.project_id}/buckets/${google_storage_bucket.action_sink.name}"
}

output "starting_principal" {
  description = "Scenario-owned identity granted object creation access."
  value       = google_service_account.runner.email
}
