# DeliveryOps on Cloud Run + Cloud SQL (Postgres 16 + pgvector).
# Creates: the database, both services, the service account, and the secret *containers*.
# Secret values are added afterwards (see deploy/README.md) so no credential touches this file.
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
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
    tier = var.db_tier
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
  replication { auto {} }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "database_url" {
  secret      = google_secret_manager_secret.app["database-url"].id
  secret_data = local.db_url
}

# ---------- runtime identity ----------

resource "google_service_account" "run" {
  account_id   = "deliveryops-run"
  display_name = "DeliveryOps Cloud Run runtime"
}

resource "google_project_iam_member" "cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.run.email}"
}

resource "google_secret_manager_secret_iam_member" "read" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

# ---------- services ----------

locals {
  shared_env = {
    DELIVERYOPS_RETRIEVER    = "pgvector"
    DELIVERYOPS_CHECKPOINTER = "postgres"
    DELIVERYOPS_LLM          = "fake" # flip to anthropic once the secret holds a real key
  }
  secret_env = {
    DATABASE_URL       = "database-url"
    ANTHROPIC_API_KEY  = "anthropic-api-key"
    JIRA_SITE          = "jira-site"
    JIRA_EMAIL         = "jira-email"
    JIRA_API_TOKEN     = "jira-api-token"
    GITHUB_TOKEN       = "github-token"
  }
}

resource "google_cloud_run_v2_service" "api" {
  name     = "deliveryops-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL" # demo: no auth in the app yet; tighten with IAP later

  template {
    service_account = google_service_account.run.email
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
  ingress  = "INGRESS_TRAFFIC_NONE"

  template {
    service_account = google_service_account.run.email
    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [local.conn_name] }
    }
    containers {
      image   = var.image
      command = ["deliveryops-worker"]
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

output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "connection_name" {
  value = local.conn_name
}
