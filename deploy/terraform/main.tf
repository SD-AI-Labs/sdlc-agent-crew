# DeliveryOps on Cloud Run + Cloud SQL (Postgres 16 + pgvector).
# Creates: the database, the API and worker services, the migrate job, per-service
# runtime identities, and the secret *containers*.
# Secret values are added afterwards (see deploy/README.md) so no credential touches this file.
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }

  # Remote state in GCS. The bucket and prefix are environment-specific, so they are
  # supplied at init time instead of being hardcoded here - one configuration serves
  # dev/staging/prod, each with its own state object:
  #   terraform init \
  #     -backend-config="bucket=<state-bucket>" \
  #     -backend-config="prefix=deliveryops/<env>"
  # State must never live on a laptop: it holds the generated database password and
  # is the only record of what exists. GCS gives locking and versioning; create the
  # bucket once per project, with uniform bucket-level access and versioning on.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  services = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
  ]
  secrets   = ["anthropic-api-key", "jira-site", "jira-email", "jira-api-token", "github-token"]
  conn_name = google_sql_database_instance.deliveryops.connection_name
  db_url    = "postgresql://deliveryops:${random_password.db.result}@/deliveryops?host=/cloudsql/${local.conn_name}"
}

resource "google_project_service" "apis" {
  for_each = toset(local.services)
  service  = each.key
}

# ---------- database ----------

resource "google_sql_database_instance" "deliveryops" {
  name                = "deliveryops"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true
  settings {
    tier              = var.db_tier
    availability_type = "REGIONAL" # zonal standby pair: a zone failure is not an outage
    disk_autoresize   = true
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      backup_retention_settings {
        retained_backups = 7
      }
    }
    maintenance_window {
      day          = 7 # Sunday
      hour         = 3 # 03:00 UTC
      update_track = "stable"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "app" {
  name     = "deliveryops"
  instance = google_sql_database_instance.deliveryops.name
}

resource "random_password" "db" {
  length  = 24
  special = false
}

resource "google_sql_user" "app" {
  name     = "deliveryops"
  instance = google_sql_database_instance.deliveryops.name
  password = random_password.db.result
}

# ---------- secrets (containers only; values come from `gcloud secrets versions add`) ----------

resource "google_secret_manager_secret" "app" {
  for_each  = toset(concat(local.secrets, ["database-url"]))
  secret_id = "deliveryops-${each.key}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "database_url" {
  secret      = google_secret_manager_secret.app["database-url"].id
  secret_data = local.db_url
}

# ---------- runtime identities (least privilege, split per service) ----------

resource "google_service_account" "api" {
  account_id   = "deliveryops-api"
  display_name = "DeliveryOps API runtime"
}

resource "google_service_account" "worker" {
  account_id   = "deliveryops-worker"
  display_name = "DeliveryOps worker / migrate runtime"
}

resource "google_project_iam_member" "cloudsql_client" {
  for_each = {
    api    = google_service_account.api.email
    worker = google_service_account.worker.email
  }
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${each.value}"
}

# The API reads every application secret.
resource "google_secret_manager_secret_iam_member" "api_read" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

# The worker (and the migrate job, which runs as the worker identity) needs only the
# database URL - no Jira, GitHub or model credentials.
resource "google_secret_manager_secret_iam_member" "worker_read_db_url" {
  secret_id = google_secret_manager_secret.app["database-url"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

# ---------- services ----------

locals {
  shared_env = {
    DELIVERYOPS_RETRIEVER    = "pgvector"
    DELIVERYOPS_CHECKPOINTER = "postgres"
    # Model layer (ADR 0008): 'chat' + provider/model env, never a provider name in
    # DELIVERYOPS_LLM. Set llm_mode = "fake" only for an offline dev deploy.
    DELIVERYOPS_LLM         = var.llm_mode
    DELIVERYOPS_PROVIDER    = var.llm_provider
    DELIVERYOPS_MODEL       = var.llm_model
    DELIVERYOPS_DB_POOL_MAX = tostring(var.db_pool_max)
  }
  secret_env = {
    DATABASE_URL      = "database-url"
    ANTHROPIC_API_KEY = "anthropic-api-key"
    JIRA_SITE         = "jira-site"
    JIRA_EMAIL        = "jira-email"
    JIRA_API_TOKEN    = "jira-api-token"
    GITHUB_TOKEN      = "github-token"
  }
}

resource "google_cloud_run_v2_service" "api" {
  name     = "deliveryops-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL" # demo: no auth in the app yet; tighten with IAP later

  template {
    service_account = google_service_account.api.email
    scaling {
      min_instance_count = 0
      # Connection budget: max_instance_count x DELIVERYOPS_DB_POOL_MAX
      # (10 x 8 = 80) must stay under the Cloud SQL tier's max_connections.
      max_instance_count = var.api_max_instances
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [local.conn_name] }
    }
    containers {
      image = var.image
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      ports { container_port = 8000 }
      dynamic "env" {
        for_each = local.shared_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_version.database_url]
}

resource "google_cloud_run_v2_service" "worker" {
  name     = "deliveryops-worker"
  location = var.region
  # No public traffic (the previous INGRESS_TRAFFIC_NONE is not a valid value for a
  # v2 service - validate rejects it; INTERNAL_ONLY is the closest legal setting).
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.worker.email
    scaling {
      # The worker is a poller, not a request handler: it must always have one
      # instance with CPU allocated, or Cloud Run throttles it to a standstill
      # between requests and gate notifications silently stop.
      min_instance_count = 1
      max_instance_count = 1
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [local.conn_name] }
    }
    containers {
      image   = var.image
      command = ["deliveryops-worker"]
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = false # CPU always allocated, not only during requests
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      dynamic "env" {
        for_each = { DATABASE_URL = "database-url" }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_version.database_url]
}

# ---------- migrations ----------

# One-shot job: applies db/schema.sql (idempotently, version-tracked) and sets up the
# LangGraph checkpointer tables. Execute it after the secrets exist and before a new
# revision takes traffic - see deploy/README.md.
resource "google_cloud_run_v2_job" "migrate" {
  name     = "deliveryops-migrate"
  location = var.region

  template {
    template {
      service_account = google_service_account.worker.email
      volumes {
        name = "cloudsql"
        cloud_sql_instance { instances = [local.conn_name] }
      }
      containers {
        image   = var.image
        command = ["deliveryops-migrate"]
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app["database-url"].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_version.database_url]
}

output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "connection_name" {
  value = local.conn_name
}
