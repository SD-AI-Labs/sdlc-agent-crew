# Deploying DeliveryOps to Cloud Run + Cloud SQL

Target: two Cloud Run services (`deliveryops-api`, `deliveryops-worker`) on Cloud SQL
Postgres 16 with pgvector. The database carries runs, audit, telemetry, the outbox **and** the
LangGraph checkpointer, so an in-flight run survives a service restart or redeploy.

`terraform/` here was written against provider `hashicorp/google ~> 6.0` but **has not been
applied yet** - run `terraform plan` and read it before `apply`.

State lives in a GCS bucket (remote backend, locking + versioning), never on a laptop:
the state holds the generated database password and is the only record of what exists.
Create the bucket once per project (`gcloud storage buckets create gs://<state-bucket>
--location=$REGION --uniform-bucket-level-access`, then enable versioning) and give each
environment its own prefix (`deliveryops/dev`, `deliveryops/staging`, `deliveryops/prod`).

## 1. Build and push the image

```bash
export PROJECT_ID=... REGION=asia-south1
gcloud artifacts repositories create deliveryops --repository-format=docker \
  --location=$REGION  # once
gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT_ID/deliveryops/app:$(git rev-parse --short HEAD)
```

## 2. Terraform

```bash
cd deploy/terraform/gcp
terraform init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="prefix=deliveryops/<env>"
terraform apply -var="project_id=$PROJECT_ID" -var="region=$REGION" \
  -var="image=$REGION-docker.pkg.dev/$PROJECT_ID/deliveryops/app:$(git rev-parse --short HEAD)"
```

This creates the secret *containers* only; no credential value is ever in Terraform files.
`database-url` is the one exception: Terraform computes it from the generated DB password.
It also creates two runtime identities: the API's service account reads every secret;
the worker's reads only `database-url` (the migrate job runs as the worker identity).

## 3. Add the real secret values

```bash
printf '%s' "$ANTHROPIC_API_KEY" | gcloud secrets versions add deliveryops-anthropic-api-key --data-file=-
printf '%s' "https://your-domain.atlassian.net" | gcloud secrets versions add deliveryops-jira-site --data-file=-
printf '%s' "you@example.com" | gcloud secrets versions add deliveryops-jira-email --data-file=-
printf '%s' "$JIRA_API_TOKEN" | gcloud secrets versions add deliveryops-jira-api-token --data-file=-
printf '%s' "$GITHUB_TOKEN" | gcloud secrets versions add deliveryops-github-token --data-file=-
```

Until a version exists the services fail to start - that is deliberate, not a bug.

## 4. Migrate the database

Run the migrate job once after the secrets exist, and again before any rollout whose
image changes the schema. It applies `src/deliveryops/db/schema.sql` idempotently
(version-tracked in `schema_migrations`) and sets up the LangGraph checkpointer
tables, so the API never boots against a half-created database:

```bash
gcloud run jobs execute deliveryops-migrate --region $REGION --wait
```

Fallback (or local debugging): the same runner works anywhere `DATABASE_URL` points -
`uv run deliveryops-migrate`. Applying `schema.sql` by hand with `psql` also still
works; the schema is written to be re-runnable.

Then ingest the knowledge library once:

```bash
cloud-sql-proxy $(terraform output -raw connection_name) &
DATABASE_URL="postgresql://deliveryops:PASSWORD@127.0.0.1/deliveryops" \
  uv run deliveryops-knowledge   # ingest the process library into pgvector
```

Note on the worker: it is a poller, so Terraform pins it to one always-on instance
with CPU always allocated (`cpu_idle = false`). Left at Cloud Run's request-driven
defaults it would be throttled to a standstill between requests and gate
notifications would silently stop - that is the cost of the durable-outbox design,
roughly one small always-on instance.

## 5. Verify

```bash
curl -s "$(terraform output -raw api_url)/health"
```

Then walk a ticket through the gates (README "Quick start"). To prove restart safety:
start a run, force a new revision (`gcloud run services update deliveryops-api
--image <same-image>`) while a gate is open, then POST the decision - the run resumes because
the checkpointer is Postgres, not process memory.
