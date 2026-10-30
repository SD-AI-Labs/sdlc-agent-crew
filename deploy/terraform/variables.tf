variable "project_id" {
  description = "GCP project id"
  type        = string
}

variable "region" {
  description = "Deploy region"
  type        = string
  default     = "asia-south1"
}

variable "image" {
  description = "Container image for the API/worker (e.g. asia-south1-docker.pkg.dev/PROJECT/deliveryops/app:SHA)"
  type        = string
}

variable "db_tier" {
  description = "Cloud SQL machine tier"
  type        = string
  default     = "db-f1-micro"
}
