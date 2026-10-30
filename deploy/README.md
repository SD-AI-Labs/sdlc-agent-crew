# Deploying DeliveryOps

One codebase, three run/deploy targets, selected by `deploy/run.sh`:

```bash
deploy/run.sh            # asks: Run target? [local/gcp/aws] (default local)
deploy/run.sh local      # this machine (compose if docker exists, else uv)
deploy/run.sh gcp        # terraform plan for Cloud Run + Cloud SQL
deploy/run.sh aws        # terraform plan for ECS Fargate + RDS
deploy/run.sh gcp --apply   # plan AND apply - the only way anything applies
```

| Target | Where it runs | State | Database |
| --- | --- | --- | --- |
| `local` | your machine | process memory or your `DATABASE_URL` | compose Postgres, or none |
| `gcp` | Cloud Run (api + worker) | GCS backend, per-env prefix | Cloud SQL Postgres 16 + pgvector |
| `aws` | ECS Fargate (api + worker behind an ALB) | S3 backend + DynamoDB lock, per-env key | RDS Postgres 16 + pgvector |

The target is also recorded in the app: each environment sets
`DELIVERYOPS_DEPLOY_TARGET`, and startup validation (`deliveryops/config.py`)
holds cloud targets to cloud rules - `DATABASE_URL` required, the offline
fake model rejected unless `DELIVERYOPS_ALLOW_FAKE_LLM=1`, and a loud warning
when no API tokens are configured. Local carries no such rules.

Both cloud modules follow the same contract: terraform creates the secret
*containers* and the `database-url` value (computed from the generated
database password); every other secret value is added afterwards with the
cloud's CLI, so no credential ever touches a terraform file. Both create
split runtime identities - the API reads every secret, the worker (and the
migrate job/task) read only `database-url`. State lives remotely, never on a
laptop: it holds the generated database password and is the only record of
what exists.

Neither module has been applied yet - run `terraform plan` (or
`deploy/run.sh <target>`, which stops at plan) and read it before `--apply`.

---

## GCP — Cloud Run + Cloud SQL

Target: two Cloud Run services (`deliveryops-api`, `deliveryops-worker`) on Cloud SQL
Postgres 16 with pgvector. The database carries runs, audit, telemetry, the outbox **and** the
LangGraph checkpointer, so an in-flight run survives a service restart or redeploy.

`terraform/gcp/` was written against provider `hashicorp/google ~> 6.0` but **has not been
applied yet** - run `terraform plan` and read it before `apply`.

State lives in a GCS bucket (remote backend, locking + versioning), never on a laptop:
create the bucket once per project (`gcloud storage buckets create gs://<state-bucket>
--location=$REGION --uniform-bucket-level-access`, then enable versioning) and give each
environment its own prefix (`deliveryops/dev`, `deliveryops/staging`, `deliveryops/prod`).

### 1. Build and push the image

```bash
export PROJECT_ID=... REGION=asia-south1
gcloud artifacts repositories create deliveryops --repository-format=docker \
  --location=$REGION  # once
gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT_ID/deliveryops/app:$(git rev-parse --short HEAD)
```

### 2. Terraform

```bash
cd deploy/terraform/gcp
terraform init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="prefix=deliveryops/<env>"
terraform apply -var="project_id=$PROJECT_ID" -var="region=$REGION" \
  -var="image=$REGION-docker.pkg.dev/$PROJECT_ID/deliveryops/app:$(git rev-parse --short HEAD)"
```

(`deploy/run.sh gcp` does the init + plan half of this, taking the backend
config from a `backend.hcl` in the module directory or from
`TF_BACKEND_CONFIG_ARGS`, and applies only with `--apply`.)

This creates the secret *containers* only; no credential value is ever in Terraform files.
`database-url` is the one exception: Terraform computes it from the generated DB password.
It also creates two runtime identities: the API's service account reads every secret;
the worker's reads only `database-url` (the migrate job runs as the worker identity).

### 3. Add the real secret values

```bash
printf '%s' "$ANTHROPIC_API_KEY" | gcloud secrets versions add deliveryops-anthropic-api-key --data-file=-
printf '%s' "https://your-domain.atlassian.net" | gcloud secrets versions add deliveryops-jira-site --data-file=-
printf '%s' "you@example.com" | gcloud secrets versions add deliveryops-jira-email --data-file=-
printf '%s' "$JIRA_API_TOKEN" | gcloud secrets versions add deliveryops-jira-api-token --data-file=-
printf '%s' "$GITHUB_TOKEN" | gcloud secrets versions add deliveryops-github-token --data-file=-
```

Until a version exists the services fail to start - that is deliberate, not a bug.

### 4. Migrate the database

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

### 5. Verify

```bash
curl -s "$(terraform output -raw api_url)/health"
```

Then walk a ticket through the gates (README "Quick start"). To prove restart safety:
start a run, force a new revision (`gcloud run services update deliveryops-api
--image <same-image>`) while a gate is open, then POST the decision - the run resumes because
the checkpointer is Postgres, not process memory.

---

## AWS — ECS Fargate + RDS

Target: two ECS Fargate services - `deliveryops-api` behind an Application Load
Balancer, `deliveryops-worker` with no load balancer - on RDS Postgres 16 with
pgvector. As on GCP, the database carries runs, audit, telemetry, the outbox and
the checkpointer, so an in-flight run survives a task restart or redeploy.

`terraform/aws/` was written against provider `hashicorp/aws ~> 5.0`, is
`terraform validate`-clean, and **has not been applied yet**.

Two AWS-specific differences from the GCP module, both stated in the module
itself: it uses the account's **default VPC** (a production deployment would
carve a dedicated VPC with private subnets; here the tasks take public IPs as
their egress path, while the database stays non-public and reachable only
from the task security groups), and the **RDS master user is the app user** -
which is also what lets `deliveryops-migrate` create the pgvector extension,
since schema.sql's `CREATE EXTENSION` needs `rds_superuser`, which the master
user holds (the GCP app user holds the Cloud SQL equivalent). The worker
needs no such privilege: Fargate allocates CPU continuously, so there is no
`cpu_idle` trap - the poller is never throttled between polls. Its container
still listens on `$PORT` (8080, serving `/health`) under the same listen
contract as everywhere else; no load balancer is attached to it.

State lives in S3 with a DynamoDB lock table, never on a laptop: create the
bucket once per account (versioning + default encryption on) and the lock
table once (hash key `LockID`, type `S`), and give each environment its own
key (`deliveryops/dev/terraform.tfstate`, `deliveryops/staging/...`,
`deliveryops/prod/...`).

### 1. Build and push the image

```bash
export AWS_REGION=ap-south-1 AWS_ACCOUNT_ID=...
# terraform apply creates the repository; for the very first push, create it beforehand:
aws ecr create-repository --repository-name deliveryops/app --region $AWS_REGION || true
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS \
  --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
docker build -t $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/deliveryops/app:$(git rev-parse --short HEAD) .
docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/deliveryops/app:$(git rev-parse --short HEAD)
```

### 2. Terraform

```bash
cd deploy/terraform/aws
terraform init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="key=deliveryops/<env>/terraform.tfstate" \
  -backend-config="dynamodb_table=<lock-table>" \
  -backend-config="region=$AWS_REGION"
terraform apply -var="region=$AWS_REGION" \
  -var="image=$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/deliveryops/app:$(git rev-parse --short HEAD)"
```

(`deploy/run.sh aws` does the init + plan half, taking the backend config
from a `backend.hcl` in the module directory or from `TF_BACKEND_CONFIG_ARGS`,
and applies only with `--apply`.)

The secret containers and identity split mirror GCP: per-service *execution*
roles do the secret injection on ECS, so the API's execution role reads every
secret and the worker's reads only `database-url`; the task roles (what the
running code could call AWS with) have no policies at all.

### 3. Add the real secret values

```bash
aws secretsmanager put-secret-value --secret-id deliveryops/anthropic-api-key --secret-string "$ANTHROPIC_API_KEY"
aws secretsmanager put-secret-value --secret-id deliveryops/jira-site --secret-string "https://your-domain.atlassian.net"
aws secretsmanager put-secret-value --secret-id deliveryops/jira-email --secret-string "you@example.com"
aws secretsmanager put-secret-value --secret-id deliveryops/jira-api-token --secret-string "$JIRA_API_TOKEN"
aws secretsmanager put-secret-value --secret-id deliveryops/github-token --secret-string "$GITHUB_TOKEN"
```

Until a value exists the tasks fail to start - that is deliberate, not a bug.

### 4. Migrate the database

Run the migrate task once after the secrets exist, and again before any
rollout whose image changes the schema:

```bash
aws ecs run-task --cluster "$(terraform output -raw cluster_name)" \
  --task-definition "$(terraform output -raw migrate_task_definition)" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$(terraform output -json subnet_ids | jq -r 'join(",")')],securityGroups=[$(terraform output -raw worker_security_group_id)],assignPublicIp=ENABLED}"
aws ecs wait tasks-stopped --cluster "$(terraform output -raw cluster_name)" --tasks <task-arn>
```

(The task ARN is in the `run-task` output; check the exit code of the
`migrate` container and its CloudWatch log stream under `/ecs/deliveryops`,
prefix `migrate`.) Fallback: `uv run deliveryops-migrate` with a
`DATABASE_URL` that can reach the instance.

Then ingest the knowledge library once, with a `DATABASE_URL` that can reach
the instance (from a host inside the VPC or over a tunnel - the instance is
not publicly accessible):

```bash
DATABASE_URL="postgresql://deliveryops:PASSWORD@<rds-endpoint>:5432/deliveryops" \
  uv run deliveryops-knowledge   # ingest the process library into pgvector
```

### 5. Verify

```bash
curl -s "$(terraform output -raw api_url)/health"
```

Then walk a ticket through the gates (README "Quick start"). To prove restart
safety: start a run, force a new deployment (`aws ecs update-service --cluster
deliveryops --service deliveryops-api --force-new-deployment`) while a gate is
open, then POST the decision - the run resumes because the checkpointer is
Postgres, not process memory.
