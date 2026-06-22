terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state — uncomment and configure for real deployments
  # backend "s3" {
  #   bucket         = "my-terraform-state"
  #   key            = "miso-elt/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "terraform-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "miso-elt"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ── Data sources ──────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" { state = "available" }

# ── Random suffix for unique names ────────────────────────────────────────────

resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  name_prefix = "miso-elt-${var.environment}"
  account_id  = data.aws_caller_identity.current.account_id
  azs         = slice(data.aws_availability_zones.available.names, 0, 2)
}

# ── Networking ────────────────────────────────────────────────────────────────

module "networking" {
  source      = "./modules/networking"
  name_prefix = local.name_prefix
  azs         = local.azs
  vpc_cidr    = var.vpc_cidr
}

# ── RDS PostgreSQL ────────────────────────────────────────────────────────────

module "rds" {
  source            = "./modules/rds"
  name_prefix       = local.name_prefix
  vpc_id            = module.networking.vpc_id
  subnet_ids        = module.networking.private_subnet_ids
  app_sg_id         = module.ecs.app_sg_id
  db_name           = var.db_name
  db_username       = var.db_username
  instance_class    = var.rds_instance_class
  multi_az          = var.environment == "production"
  deletion_protection = var.environment == "production"
}

# ── ECS (Fargate) ─────────────────────────────────────────────────────────────

module "ecs" {
  source            = "./modules/ecs"
  name_prefix       = local.name_prefix
  vpc_id            = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  public_subnet_ids  = module.networking.public_subnet_ids
  aws_region        = var.aws_region
  account_id        = local.account_id
  environment       = var.environment

  # Secrets (stored in Secrets Manager, injected as env vars)
  db_secret_arn      = module.rds.db_secret_arn
  api_key_secret_arn = aws_secretsmanager_secret.api_key.arn
  sns_topic_arn      = module.monitoring.sns_topic_arn

  db_host = module.rds.db_endpoint
  db_name = var.db_name
  db_username = var.db_username

  # ECR image — set via TF_VAR or CI pipeline
  ecr_image_uri     = var.ecr_image_uri
}

# ── Monitoring & Alerting ──────────────────────────────────────────────────────

module "monitoring" {
  source          = "./modules/monitoring"
  name_prefix     = local.name_prefix
  aws_region      = var.aws_region
  environment     = var.environment
  alert_email     = var.alert_email
  ecs_cluster_name = module.ecs.cluster_name
  api_service_name = module.ecs.api_service_name
  rds_identifier  = module.rds.db_identifier
  log_group_names = module.ecs.log_group_names
}

# ── API key (Secrets Manager) ──────────────────────────────────────────────────

resource "random_password" "api_key" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "api_key" {
  name                    = "${local.name_prefix}/api-key"
  recovery_window_in_days = 0   # allow immediate deletion in dev
}

resource "aws_secretsmanager_secret_version" "api_key" {
  secret_id     = aws_secretsmanager_secret.api_key.id
  secret_string = random_password.api_key.result
}
