# Deploying DeliveryOps to Cloud Run + Cloud SQL

Target: two Cloud Run services (`deliveryops-api`, `deliveryops-worker`) on Cloud SQL
Postgres 16 with pgvector. The database carries runs, audit, telemetry, the outbox **and** the
LangGraph checkpointer, so an in-flight run survives a service restart or redeploy.

`terraform/` here was written against provider `hashicorp/google ~> 6.0` but **has not been
applied yet** - run `terraform plan` and read it before `apply`.

## 1. Build and push the image

```bash
export PROJECT_ID=... REGION=asia-south1
gcloud artifacts repositories create deliveryops --repository-format=docker \
  --location=$REGION  # once
gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT_ID/deliveryops/app:$(git rev-parse --short HEAD)
```

## 2. Terraform

```bash
cd deploy/terraform
terraform init
terraform apply -var="project_id=$PROJECT_ID" -var="region=$REGION" \
  -var="image=$REGION-docker.pkg.dev/$PROJECT_ID/deliveryops/app:$(git rev-parse --short HEAD)"
```

This creates the secret *containers* only; no credential value is ever in Terraform files.
`database-url` is the one exception: Terraform computes it from the generated DB password.

## 3. Add the real secret values

```bash
printf '%s' "$ANTHROPIC_API_KEY" | gcloud secrets versions add deliveryops-anthropic-api-key --data-file=-
printf '%s' "https://your-domain.atlassian.net" | gcloud secrets versions add deliveryops-jira-site --data-file=-
printf '%s' "you@example.com" | gcloud secrets versions add deliveryops-jira-email --data-file=-
printf '%s' "$JIRA_API_TOKEN" | gcloud secrets versions add deliveryops-jira-api-token --data-file=-
printf '%s' "$GITHUB_TOKEN" | gcloud secrets versions add deliveryops-github-token --data-file=-
```

Until a version exists the services fail to start - that is deliberate, not a bug.

## 4. One-time database setup

Cloud SQL does not run init scripts, so apply the schema and extension once through the proxy:

```bash
cloud-sql-proxy $(terraform output -raw connection_name) &
psql "host=127.0.0.1 user=deliveryops dbname=deliveryops" \
  -c "CREATE EXTENSION IF NOT EXISTS vector;" \
  -f ../../src/deliveryops/db/schema.sql
DATABASE_URL="postgresql://deliveryops:PASSWORD@127.0.0.1/deliveryops" \
  uv run deliveryops-knowledge   # ingest the process library into pgvector
```

## 5. Verify

```bash
curl -s "$(terraform output -raw api_url)/health"
```

Then walk a ticket through the gates (README "Quick start"). To prove restart safety:
start a run, force a new revision (`gcloud run services update deliveryops-api
--image <same-image>`) while a gate is open, then POST the decision - the run resumes because
the checkpointer is Postgres, not process memory.
