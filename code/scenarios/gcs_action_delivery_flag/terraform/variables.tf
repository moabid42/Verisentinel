variable "project_id" {
  description = "Google Cloud project that owns the scenario resources."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a canonical Google Cloud project ID."
  }
}

variable "bucket_name" {
  description = "Globally unique Cloud Storage bucket declared by the scenario."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must be a canonical Cloud Storage bucket name."
  }
}

variable "location" {
  description = "Cloud Storage region or multi-region for the scenario bucket."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9-]{0,31}$", var.location))
    error_message = "location must be a canonical Cloud Storage location."
  }
}

variable "starting_principal" {
  description = "Exact service-account identity declared by the scenario."
  type        = string

  validation {
    condition = can(regex(
      "^[^@[:space:]]+@[^@[:space:]]+\\.iam\\.gserviceaccount\\.com$",
      var.starting_principal,
    ))
    error_message = "starting_principal must be a service-account email."
  }
}

variable "provisioner_member" {
  description = "Verified ADC principal allowed to impersonate the scenario identity."
  type        = string

  validation {
    condition = can(regex(
      "^(user|serviceAccount):[^@[:space:]]+@[^@[:space:]]+$",
      var.provisioner_member,
    ))
    error_message = "provisioner_member must be one user or service-account member."
  }
}
