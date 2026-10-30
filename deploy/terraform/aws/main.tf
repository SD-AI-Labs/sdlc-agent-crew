# DeliveryOps on ECS Fargate + RDS (Postgres 16 + pgvector).
# Creates: the database, the ECR repository, an ALB-fronted API service, the worker
# service, the migrate task definition, per-service runtime identities, and the
# secret *containers*.
# Secret values are added afterwards (see deploy/README.md) so no credential touches
# this file - the same contract as the GCP module.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }

  # Remote state in S3 with DynamoDB locking. Bucket, key and table are
  # environment-specific, so they are supplied at init time instead of being
  # hardcoded here - one configuration serves dev/staging/prod, each with its
  # own state object:
  #   terraform init \
  #     -backend-config="bucket=<state-bucket>" \
  #     -backend-config="key=deliveryops/<env>/terraform.tfstate" \
  #     -backend-config="dynamodb_table=<lock-table>" \
  #     -backend-config="region=<region>"
  # State must never live on a laptop: it holds the generated database password
  # and is the only record of what exists. Create the bucket once per account
  # (versioning on, default encryption) and the lock table once (hash key
  # LockID, type S).
  backend "s3" {}
}

provider "aws" {
  region = var.region
}

# ---------- network ----------
#
# SIMPLIFICATION, stated plainly: this module uses the account's *default* VPC
# and its subnets to stay small. A production deployment would carve a dedicated
# VPC with private subnets for the tasks and database, NAT (or VPC endpoints)
# for egress, and no public IPs on tasks. What that simplification costs here:
# Fargate tasks run in the default (public) subnets with public IPs assigned -
# that is their egress path to ECR, Secrets Manager and the model APIs. The
# database is still not publicly accessible and its security group admits 5432
# only from the task security groups; the tasks' own inbound is restricted the
# same way (API tasks accept traffic only from the ALB).

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

locals {
  secrets = ["anthropic-api-key", "jira-site", "jira-email", "jira-api-token", "github-token"]
  db_url  = "postgresql://deliveryops:${random_password.db.result}@${aws_db_instance.deliveryops.address}:5432/deliveryops"
}

# ---------- image registry ----------

resource "aws_ecr_repository" "app" {
  name                 = "deliveryops/app"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

# ---------- database ----------

resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_db_subnet_group" "deliveryops" {
  name       = "deliveryops"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_security_group" "rds" {
  name        = "deliveryops-rds"
  description = "DeliveryOps RDS: 5432 only from the task security groups"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_api" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres from API tasks"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.api_tasks.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_worker" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres from worker/migrate tasks"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.worker_tasks.id
}

resource "aws_db_instance" "deliveryops" {
  identifier     = "deliveryops"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 100 # storage autoscaling ceiling
  storage_encrypted     = true

  db_name  = "deliveryops"
  username = "deliveryops"
  password = random_password.db.result

  # The master user carries rds_superuser, which is what lets deliveryops-migrate
  # run schema.sql's CREATE EXTENSION for pgvector (see deploy/README.md). The
  # GCP module's app user holds the equivalent privilege on Cloud SQL.
  publicly_accessible    = false
  db_subnet_group_name   = aws_db_subnet_group.deliveryops.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  multi_az                = var.db_multi_az
  backup_retention_period = 7             # automated backups; PITR restore to any second in the window
  backup_window           = "18:00-19:00" # UTC; 23:30-00:30 IST
  maintenance_window      = "sun:19:30-sun:20:30"

  deletion_protection = true
  # Final snapshots are skipped on destroy: deletion_protection is the guard,
  # and automated backups already retain restore points.
  skip_final_snapshot = true
}

# ---------- secrets (containers only; values come from `aws secretsmanager put-secret-value`) ----------

resource "aws_secretsmanager_secret" "app" {
  for_each = toset(concat(local.secrets, ["database-url"]))
  name     = "deliveryops/${each.key}"
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.app["database-url"].id
  secret_string = local.db_url
}

# ---------- runtime identities (least privilege, split per service) ----------
#
# The AWS mapping of the GCP module's service-account split: on ECS, secrets
# reach a container through its *execution* role (the agent that pulls the
# image, injects secrets and ships logs), so the split lives there. Task roles
# (what the application code itself could call AWS with) get no policies at
# all - the app makes no AWS API calls.

resource "aws_iam_role" "execution_api" {
  name = "deliveryops-execution-api"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role" "execution_worker" {
  name = "deliveryops-execution-worker"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution_api_managed" {
  role       = aws_iam_role.execution_api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy_attachment" "execution_worker_managed" {
  role       = aws_iam_role.execution_worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The API's execution role may inject every application secret.
resource "aws_iam_role_policy" "execution_api_secrets" {
  name = "deliveryops-read-secrets"
  role = aws_iam_role.execution_api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [for s in aws_secretsmanager_secret.app : s.arn]
    }]
  })
}

# The worker/migrate execution role may inject only the database URL - no Jira,
# GitHub or model credentials, exactly as on GCP.
resource "aws_iam_role_policy" "execution_worker_secrets" {
  name = "deliveryops-read-database-url"
  role = aws_iam_role.execution_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.app["database-url"].arn]
    }]
  })
}

resource "aws_iam_role" "task_api" {
  name = "deliveryops-task-api"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role" "task_worker" {
  name = "deliveryops-task-worker"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# ---------- logging ----------

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/deliveryops"
  retention_in_days = 30
}

# ---------- task + load balancer security groups ----------

resource "aws_security_group" "alb" {
  name        = "deliveryops-alb"
  description = "DeliveryOps ALB: public HTTP in, API tasks out"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from anywhere (demo posture, like the GCP module's INGRESS_TRAFFIC_ALL; tighten with a WAF/allowlist or put SSO in front)"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "alb_all" {
  security_group_id = aws_security_group.alb.id
  description       = "ALB egress (to API tasks)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "api_tasks" {
  name        = "deliveryops-api-tasks"
  description = "DeliveryOps API tasks: 8000 only from the ALB"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "api_from_alb" {
  security_group_id            = aws_security_group.api_tasks.id
  description                  = "API port from the ALB"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.alb.id
}

resource "aws_vpc_security_group_egress_rule" "api_all" {
  security_group_id = aws_security_group.api_tasks.id
  description       = "Task egress (RDS, ECR, Secrets Manager, model APIs)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "worker_tasks" {
  name        = "deliveryops-worker-tasks"
  description = "DeliveryOps worker/migrate tasks: no inbound, all egress"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_egress_rule" "worker_all" {
  security_group_id = aws_security_group.worker_tasks.id
  description       = "Task egress (RDS, ECR, Secrets Manager, Slack)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ---------- load balancer (API only; the worker has none) ----------

resource "aws_lb" "api" {
  name               = "deliveryops-api"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids
}

resource "aws_lb_target_group" "api" {
  name        = "deliveryops-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id

  health_check {
    path    = "/health"
    matcher = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# ---------- services ----------

resource "aws_ecs_cluster" "deliveryops" {
  name = "deliveryops"
}

locals {
  shared_env = [
    { name = "DELIVERYOPS_DEPLOY_TARGET", value = "aws" },
    { name = "DELIVERYOPS_RETRIEVER", value = "pgvector" },
    { name = "DELIVERYOPS_CHECKPOINTER", value = "postgres" },
    # Model layer (ADR 0008): 'chat' + provider/model env, never a provider
    # name in DELIVERYOPS_LLM. Set llm_mode = "fake" only for an offline dev
    # deploy (config validation then also asks for DELIVERYOPS_ALLOW_FAKE_LLM).
    { name = "DELIVERYOPS_LLM", value = var.llm_mode },
    { name = "DELIVERYOPS_PROVIDER", value = var.llm_provider },
    { name = "DELIVERYOPS_MODEL", value = var.llm_model },
    { name = "DELIVERYOPS_DB_POOL_MAX", value = tostring(var.db_pool_max) },
  ]
  api_secrets = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.app["database-url"].arn },
    { name = "ANTHROPIC_API_KEY", valueFrom = aws_secretsmanager_secret.app["anthropic-api-key"].arn },
    { name = "JIRA_SITE", valueFrom = aws_secretsmanager_secret.app["jira-site"].arn },
    { name = "JIRA_EMAIL", valueFrom = aws_secretsmanager_secret.app["jira-email"].arn },
    { name = "JIRA_API_TOKEN", valueFrom = aws_secretsmanager_secret.app["jira-api-token"].arn },
    { name = "GITHUB_TOKEN", valueFrom = aws_secretsmanager_secret.app["github-token"].arn },
  ]
  db_secret = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.app["database-url"].arn },
  ]
}

resource "aws_ecs_task_definition" "api" {
  family                   = "deliveryops-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution_api.arn
  task_role_arn            = aws_iam_role.task_api.arn

  container_definitions = jsonencode([{
    name    = "api"
    image   = var.image
    command = ["deliveryops-api"]
    portMappings = [{
      containerPort = 8000
      hostPort      = 8000
      protocol      = "tcp"
    }]
    environment = concat(local.shared_env, [{ name = "PORT", value = "8000" }])
    secrets     = local.api_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "deliveryops-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution_worker.arn
  task_role_arn            = aws_iam_role.task_worker.arn

  container_definitions = jsonencode([{
    name    = "worker"
    image   = var.image
    command = ["deliveryops-worker"]
    # No load balancer: the port exists so the process satisfies the same
    # "listen on $PORT" contract as on Cloud Run (it serves GET /health).
    # Fargate allocates CPU continuously, so there is no cpu_idle concept -
    # the poller is never throttled between polls.
    portMappings = [{
      containerPort = 8080
      hostPort      = 8080
      protocol      = "tcp"
    }]
    environment = concat(local.shared_env, [{ name = "PORT", value = "8080" }])
    secrets     = local.db_secret
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
}

# One-shot migration task: same image, deliveryops-migrate command, the worker's
# identity and network. Run it once after the secrets exist - deploy/README.md
# has the `aws ecs run-task` command, built from this module's outputs.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "deliveryops-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution_worker.arn
  task_role_arn            = aws_iam_role.task_worker.arn

  container_definitions = jsonencode([{
    name        = "migrate"
    image       = var.image
    command     = ["deliveryops-migrate"]
    environment = local.shared_env
    secrets     = local.db_secret
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "migrate"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "deliveryops-api"
  cluster         = aws_ecs_cluster.deliveryops.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.api_tasks.id]
    assign_public_ip = true # default-VPC egress path; see the network note above
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http, aws_secretsmanager_secret_version.database_url]
}

resource "aws_ecs_service" "worker" {
  name            = "deliveryops-worker"
  cluster         = aws_ecs_cluster.deliveryops.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.worker_tasks.id]
    assign_public_ip = true # default-VPC egress path; see the network note above
  }

  depends_on = [aws_secretsmanager_secret_version.database_url]
}

# ---------- outputs ----------

output "api_url" {
  value = "http://${aws_lb.api.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "cluster_name" {
  value = aws_ecs_cluster.deliveryops.name
}

output "migrate_task_definition" {
  value = aws_ecs_task_definition.migrate.arn
}

output "worker_security_group_id" {
  value = aws_security_group.worker_tasks.id
}

output "subnet_ids" {
  value = data.aws_subnets.default.ids
}
