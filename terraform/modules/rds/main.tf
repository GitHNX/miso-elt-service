# modules/rds/main.tf
# RDS PostgreSQL 16 in private subnets.
# Master password stored in Secrets Manager (RDS managed rotation).
# No public accessibility — only reachable from within the VPC.

variable "name_prefix"         { type = string }
variable "vpc_id"              { type = string }
variable "subnet_ids"          { type = list(string) }
variable "app_sg_id"           { type = string }
variable "db_name"             { type = string }
variable "db_username"         { type = string }
variable "instance_class"      { type = string }
variable "multi_az"            { type = bool    default = false }
variable "deletion_protection" { type = bool    default = false }

# ── Security group ────────────────────────────────────────────────────────────

resource "aws_security_group" "rds" {
  name_prefix = "${var.name_prefix}-rds-"
  vpc_id      = var.vpc_id
  description = "RDS PostgreSQL — accepts connections only from ECS tasks"

  ingress {
    description     = "PostgreSQL from ECS"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.app_sg_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle { create_before_destroy = true }
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.name_prefix}-db-subnet-group"
  subnet_ids = var.subnet_ids
}

# ── Parameter group ───────────────────────────────────────────────────────────

resource "aws_db_parameter_group" "main" {
  name_prefix = "${var.name_prefix}-pg16-"
  family      = "postgres16"

  parameter {
    name  = "log_connections"
    value = "1"
  }
  parameter {
    name  = "log_disconnections"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"   # log queries taking > 1 s
  }

  lifecycle { create_before_destroy = true }
}

# ── RDS instance ──────────────────────────────────────────────────────────────

resource "aws_db_instance" "main" {
  identifier        = "${var.name_prefix}-postgres"
  engine            = "postgres"
  engine_version    = "16"
  instance_class    = var.instance_class
  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username

  # RDS-managed secret rotation — no plaintext password in Terraform state
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.main.name

  multi_az                = var.multi_az
  publicly_accessible     = false
  deletion_protection     = var.deletion_protection
  skip_final_snapshot     = !var.deletion_protection
  final_snapshot_identifier = var.deletion_protection ? "${var.name_prefix}-final-snapshot" : null

  backup_retention_period = 7
  backup_window           = "03:00-04:00"
  maintenance_window      = "Mon:04:00-Mon:05:00"

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  tags = { Name = "${var.name_prefix}-postgres" }
}

output "db_endpoint"   { value = aws_db_instance.main.address }
output "db_port"       { value = aws_db_instance.main.port }
output "db_secret_arn" { value = aws_db_instance.main.master_user_secret[0].secret_arn }
output "db_identifier" { value = aws_db_instance.main.identifier }
