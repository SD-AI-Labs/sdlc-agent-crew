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

variable "llm_mode" {
  description = "DELIVERYOPS_LLM: 'chat' for a real provider (default), 'fake' for the offline keyword model"
  type        = string
  default     = "chat"
}

variable "llm_provider" {
  description = "DELIVERYOPS_PROVIDER: model provider used when llm_mode is 'chat'"
  type        = string
  default     = "anthropic"
}

variable "llm_model" {
  description = "DELIVERYOPS_MODEL: model name passed to the provider"
  type        = string
  default     = "claude-sonnet-4-6"
}

variable "api_max_instances" {
  description = "Max Cloud Run instances for the API. Connection budget: api_max_instances x db_pool_max must stay under the Cloud SQL tier's max_connections, with headroom."
  type        = number
  default     = 10
}

variable "db_pool_max" {
  description = "DELIVERYOPS_DB_POOL_MAX: max pooled Postgres connections per API instance"
  type        = number
  default     = 8
}
