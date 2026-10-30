variable "region" {
  description = "Deploy region (ap-south-1 = Mumbai, the AWS mirror of the GCP module's asia-south1 default)"
  type        = string
  default     = "ap-south-1"
}

variable "image" {
  description = "Container image for api/worker/migrate (e.g. <account>.dkr.ecr.ap-south-1.amazonaws.com/deliveryops/app:SHA). The ECR repository itself is created by this module; push before the services can start."
  type        = string
}

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t4g.micro"
}

variable "db_multi_az" {
  description = "RDS Multi-AZ standby (the Cloud SQL module's REGIONAL availability, mirrored). Roughly doubles the database cost."
  type        = bool
  default     = true
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

variable "db_pool_max" {
  description = "DELIVERYOPS_DB_POOL_MAX: max pooled Postgres connections per task"
  type        = number
  default     = 8
}

variable "task_cpu" {
  description = "Fargate task CPU units per service (1024 = 1 vCPU)"
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate task memory (MiB) per service"
  type        = number
  default     = 1024
}
